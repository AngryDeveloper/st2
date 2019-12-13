#! /usr/bin/python

# Copyright 2019 Extreme Networks, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import errno
import subprocess
import random
import re

from st2common.runners.base_action import Action


class DigAction(Action):

    def run(self, rand, count, nameserver, hostname, queryopts):
        opt_list = []
        output = []

        cmd_args = ['dig']
        if nameserver:
            nameserver = '@' + nameserver
            cmd_args.append(nameserver)

        if re.search(',', queryopts):
            opt_list = queryopts.split(',')
        else:
            opt_list.append(queryopts)
        for k, v in enumerate(opt_list):
            cmd_args.append('+' + v)

        cmd_args.append(hostname)

        try:
            result_list = filter(None, subprocess.Popen(cmd_args,
                                                        stderr=subprocess.PIPE,
                                                        stdout=subprocess.PIPE)
                                 .communicate()[0]
                                 .split('\n'))

        # NOTE: Python3 supports the FileNotFoundError, the errono.ENOENT is for py2 compat
        # for Python3:
        # except FileNotFoundError as e:

        except OSError as e:
            if e.errno == errno.ENOENT:
                return False, "Can't find dig installed in the path (usually /usr/bin/dig). If " \
                              "dig isn't installed, you can install it with 'sudo yum install " \
                              "bind-utils' or 'sudo apt install dnsutils'"
            else:
                raise e

        if int(count) > len(result_list) or count <= 0:
            count = len(result_list)

        output = result_list[0:count]
        if rand is True:
            random.shuffle(output)
        return output
