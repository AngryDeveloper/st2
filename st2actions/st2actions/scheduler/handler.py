# Licensed to the StackStorm, Inc ('StackStorm') under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import absolute_import

import eventlet
from oslo_config import cfg

from st2common import log as logging
from st2common.util import date
from st2common.constants import action as action_constants
from st2common.exceptions.db import StackStormDBObjectNotFoundError
from st2common.models.db.liveaction import LiveActionDB
from st2common.services import action as action_service
from st2common.services import policies as policy_service
from st2common.persistence.liveaction import LiveAction
from st2common.persistence.execution_queue import ActionExecutionSchedulingQueue
from st2common.util import action_db as action_utils
from st2common.metrics import base as metrics
from st2common.exceptions import db as db_exc

__all__ = [
    'ActionExecutionSchedulingQueueHandler',
    'get_handler'
]


LOG = logging.getLogger(__name__)

# If an ActionExecutionSchedulingQueueItemDB object hasn't been updated fore more than this amount
# of milliseconds, it will be marked as "handled=False".
# As soon as an item is picked by scheduler to be processed, it should be processed very fast
# (< 5 seconds). If an item is still being marked as processing it likely indicates that the
# scheduler process which was processing that item crashed or similar so we need to mark it as
# "handling=False" so some other scheduler process can pick it up.
EXECUTION_SCHEDUELING_TIMEOUT_THRESHOLD_MS = (60 * 1000)

# When a policy delayed execution is detected it will be try to be rescheduled by the scheduler
# again in this amount of milliseconds.
POLICY_DELAYED_EXECUTION_RESCHEDULE_TIME_MS = 1500


class ActionExecutionSchedulingQueueHandler(object):
    def __init__(self):
        self.message_type = LiveActionDB
        self._shutdown = False
        self._pool = eventlet.GreenPool(size=cfg.CONF.scheduler.pool_size)

    def run(self):
        LOG.debug('Entering scheduler loop')

        while not self._shutdown:
            eventlet.greenthread.sleep(cfg.CONF.scheduler.sleep_interval)

            execution_queue_item_db = self._get_next_execution()

            if execution_queue_item_db:
                self._pool.spawn(self._handle_execution, execution_queue_item_db)

    def cleanup(self):
        LOG.debug('Starting scheduler garbage collection')

        while not self._shutdown:
            eventlet.greenthread.sleep(cfg.CONF.scheduler.gc_interval)
            self._handle_garbage_collection()

    def _handle_garbage_collection(self):
        """
        Periodically look for executions which have "handling" set to "True" and haven't been
        updated for a while (this likely indicates that an execution as picked up by a scheduler
        process which died before finishing the processing or similar) and reset handling to
        False so other scheduler can pick it up.
        """
        query = {
            'scheduled_start_timestamp__lte': date.append_milliseconds_to_time(
                date.get_datetime_utc_now(),
                -EXECUTION_SCHEDUELING_TIMEOUT_THRESHOLD_MS
            ),
            'handling': True
        }

        execution_queue_item_dbs = ActionExecutionSchedulingQueue.query(**query) or []

        for execution_queue_item_db in execution_queue_item_dbs:
            execution_queue_item_db.handling = False

            try:
                ActionExecutionSchedulingQueue.add_or_update(execution_queue_item_db, publish=False)
                LOG.info('Removing lock for orphaned execution queue item: %s',
                         execution_queue_item_db.id)
            except db_exc.StackStormDBObjectWriteConflictError:
                LOG.info(
                    'Execution queue item updated before rescheduling: %s',
                    execution_queue_item_db.id
                )

    # NOTE: This method call is intentionally not instrumented since it causes too much overhead
    # and noise under DEBUG log level
    def _get_next_execution(self):
        """
        Sort execution requests by FIFO and priority and get the latest, highest priority item from
        the queue and pop it off.
        """
        query = {
            'scheduled_start_timestamp__lte': date.get_datetime_utc_now(),
            'handling': False,
            'limit': 1,
            'order_by': [
                '+scheduled_start_timestamp',
            ]
        }

        execution_queue_item_db = ActionExecutionSchedulingQueue.query(**query).first()

        if not execution_queue_item_db:
            return None

        # Mark that this scheduler process is currently handling (processing) that request
        # NOTE: This operation is atomic (CAS)
        execution_queue_item_db.handling = True

        try:
            ActionExecutionSchedulingQueue.add_or_update(execution_queue_item_db, publish=False)
            return execution_queue_item_db
        except db_exc.StackStormDBObjectWriteConflictError:
            LOG.info('Execution queue item handled by another scheduler: %s',
                     execution_queue_item_db.id)

        return None

    @metrics.CounterWithTimer(key='scheduler.handle_execution')
    def _handle_execution(self, execution_queue_item_db):
        liveaction_id = str(execution_queue_item_db.liveaction_id)
        queue_item_id = str(execution_queue_item_db.id)

        extra = {
            'liveaction_id': liveaction_id,
            'queue_item_id': queue_item_id
        }

        LOG.info('Scheduling liveaction: %s (queue_item_id=%s)', liveaction_id,
                 queue_item_id, extra=extra)

        try:
            liveaction_db = action_utils.get_liveaction_by_id(liveaction_id)
        except StackStormDBObjectNotFoundError:
            LOG.exception('Failed to find liveaction %s in the database (queue_item_id=%s).',
                          liveaction_id, queue_item_id, extra=extra)
            ActionExecutionSchedulingQueue.delete(execution_queue_item_db)
            raise

        liveaction_db = self._apply_pre_run(liveaction_db, execution_queue_item_db)

        if not liveaction_db:
            return

        if self._is_execution_queue_item_runnable(liveaction_db, execution_queue_item_db):
            self._update_to_scheduled(liveaction_db, execution_queue_item_db)

    @staticmethod
    def _apply_pre_run(liveaction_db, execution_queue_item_db):
        # Apply policies defined for the action.
        liveaction_db = policy_service.apply_pre_run_policies(liveaction_db)

        liveaction_id = str(liveaction_db.id)
        queue_item_id = str(execution_queue_item_db.id)

        extra = {
            'liveaction_id': liveaction_id,
            'liveaction_status': liveaction_db.status,
            'queue_item_id': queue_item_id
        }

        LOG.info('Liveaction (%s) Status Pre-Run: %s (%s)', liveaction_id, liveaction_db.status,
                 queue_item_id, extra=extra)

        if liveaction_db.status is action_constants.LIVEACTION_STATUS_POLICY_DELAYED:
            liveaction_db = action_service.update_status(
                liveaction_db, action_constants.LIVEACTION_STATUS_DELAYED, publish=False
            )
            execution_queue_item_db.scheduled_start_timestamp = date.append_milliseconds_to_time(
                date.get_datetime_utc_now(),
                POLICY_DELAYED_EXECUTION_RESCHEDULE_TIME_MS
            )
            try:
                ActionExecutionSchedulingQueue.add_or_update(execution_queue_item_db, publish=False)
            except db_exc.StackStormDBObjectWriteConflictError:
                LOG.warning(
                    'Execution queue item update conflict during scheduling: %s',
                    execution_queue_item_db.id
                )

            return None

        if (liveaction_db.status in action_constants.LIVEACTION_COMPLETED_STATES or
                liveaction_db.status in action_constants.LIVEACTION_CANCEL_STATES):
            ActionExecutionSchedulingQueue.delete(execution_queue_item_db)
            return None

        return liveaction_db

    def _is_execution_queue_item_runnable(self, liveaction_db, execution_queue_item_db):
        """
        Return True if a particular execution request is runnable.

        The status of the liveaction could have been changed by one of the policies and that could
        make execution not runnable anymore.
        """
        valid_status = [
            action_constants.LIVEACTION_STATUS_REQUESTED,
            action_constants.LIVEACTION_STATUS_SCHEDULED,
            action_constants.LIVEACTION_STATUS_DELAYED
        ]

        if liveaction_db.status in valid_status:
            return True

        LOG.info(
            '%s is ignoring %s (id=%s) with "%s" status after policies are applied.',
            self.__class__.__name__,
            type(execution_queue_item_db),
            execution_queue_item_db.id,
            liveaction_db.status
        )
        ActionExecutionSchedulingQueue.delete(execution_queue_item_db)
        return False

    @staticmethod
    def _update_to_scheduled(liveaction_db, execution_queue_item_db):
        liveaction_id = str(liveaction_db.id)
        queue_item_id = str(execution_queue_item_db.id)

        extra = {
            'liveaction_id': liveaction_id,
            'liveaction_status': liveaction_db.status,
            'queue_item_id': queue_item_id
        }

        # Update liveaction status to "scheduled".
        LOG.info('Liveaction (%s) Status Update to Scheduled 1: %s (%s)',
                liveaction_id, liveaction_db.status, queue_item_id, extra=extra)

        if liveaction_db.status in [action_constants.LIVEACTION_STATUS_REQUESTED,
                                    action_constants.LIVEACTION_STATUS_DELAYED]:
            liveaction_db = action_service.update_status(
                liveaction_db, action_constants.LIVEACTION_STATUS_SCHEDULED, publish=False)

        # Publish the "scheduled" status here manually. Otherwise, there could be a
        # race condition with the update of the action_execution_db if the execution
        # of the liveaction completes first.
        LiveAction.publish_status(liveaction_db)

        extra['liveaction_status'] = liveaction_db.status

        # Delete execution queue entry only after status is published.
        ActionExecutionSchedulingQueue.delete(execution_queue_item_db)
        LOG.info('Liveaction (%s) Status Update to Scheduled 2: %s (%s)',
                liveaction_id, liveaction_db.status, queue_item_id)

    def start(self):
        self._shutdown = False

        eventlet.spawn(self.run)
        eventlet.spawn(self.cleanup)

    def shutdown(self):
        self._shutdown = True


def get_handler():
    return ActionExecutionSchedulingQueueHandler()
