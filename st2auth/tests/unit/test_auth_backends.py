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

import os

import unittest2

from st2auth.backends import get_available_backends

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


class AuthenticationBackendsTestCase(unittest2.TestCase):
    def test_flat_file_backend_is_available_by_default(self):
        available_backends = get_available_backends()
        self.assertTrue('flat_file' in available_backends)
