#
# Copyright (c) 2024 NVIDIA CORPORATION & AFFILIATES.
# Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

from . import utils
from .device_data import DeviceDataManager
from sonic_py_common.logger import Logger

import atexit
import os
import sys
import threading
import re
import fcntl

MODULE_READY_MAX_WAIT_TIME = 300
MODULE_READY_CHECK_INTERVAL = 5
ASIC_READY_CONTAINER_FILE = '/tmp/module_host_mgmt_asic_ready'
MODULE_READY_HOST_FILE = '/tmp/nv-syncd-shared/module_host_mgmt_ready'
DEDICATE_INIT_DAEMON = 'xcvrd'
initialization_owner = False

logger = Logger()


class ModuleHostMgmtInitializer:
    """Responsible for initializing modules for host management mode.
    """
    def __init__(self):
        self.initialized = False
        self.lock = threading.Lock()
        self.asic_count = DeviceDataManager.get_asic_count()
        self.initialized_list = [False] * self.asic_count
        open(ASIC_READY_CONTAINER_FILE, 'a').close()

    def initialize(self, chassis):
        """Initialize all modules. Only applicable for module host management mode.
        The real initialization job shall only be done in xcvrd. Only 1 owner is allowed
        to to the initialization. Other daemon/CLI shall wait for the initialization done.

        Args:
            chassis (object): chassis object
        """
        global initialization_owner
        not_initialized = []
        for i in range(self.initialized_list):
            if not self.initialized_list[i]:
                not_initialized.append(i)

        if not not_initialized:
            return

        if utils.is_host():
            chassis.initialize_sfp()
        else:
            if self.is_initialization_owner():
                if not_initialized:
                    with self.lock:
                        # Double check if the asics are not available
                        not_initialized = []
                        for i in range(self.initialized_list):
                            if not self.initialized_list[i]:
                                not_initialized.append(i)

                        if not_initialized:
                            # TODO is it really necessary? check with Sasha
                            logger.log_notice('Waiting for modules to be ready...')
                            sfp_count = chassis.get_num_sfps()
                            if not DeviceDataManager.wait_sysfs_ready(sfp_count):
                                logger.log_error('Modules are not ready')
                            else:
                                logger.log_notice('Modules are ready')

                            logger.log_notice('Starting module initialization for module host management...')
                            initialization_owner = True
                            self.remove_asics_from_ready_file(not_initialized)
                            chassis.initialize_sfp()
                            asic_ready_list = []
                            sfp_list = []
                            for asic_id in not_initialized:
                                if utils.read_int_from_file(f'/var/run/hw-management/config/asic{asic_id}_ready') == 1:
                                    asic_ready_list.append(asic_id)
                                    sfp_list.extend(chassis._asic_modules_dict[asic_id])
                            from .sfp import SFP
                            if sfp_list:
                                SFP.initialize_sfp_modules(sfp_list)
                                self.add_asics_to_ready_file(asic_ready_list)
                                for asic_id in asic_ready_list:
                                    self.initialized_list[asic_id] = True
                                logger.log_notice('Module initialization for module host management done')
            else:
                chassis.initialize_sfp()

    def remove_asics_from_ready_file(self, asic_ids):
        """
        Remove Asic IDs from the asic ready file
        check for asic{id} in the file and rewrite the file without the matched lines.
        Args:
            asic_ids (list): list of asic ids to remove (numbers)

        """
        asics_to_remove = re.compile(rf'\b(?:{"|".join(f"asic{asic_id}" for asic_id in asic_ids)})\b')
        with open(ASIC_READY_CONTAINER_FILE, 'r') as file:
            fcntl.flock(file.fileno(), fcntl.LOCK_SH)
            try:
                asic_lines = file.readlines()
            finally:
                fcntl.flock(file.fileno(), fcntl.LOCK_UN)

        with open(ASIC_READY_CONTAINER_FILE, 'w') as file:
            fcntl.flock(file.fileno(), fcntl.LOCK_EX)
            try:
            for line in asic_lines:
                    if not asics_to_remove.match(line):
                        file.write(line)
            finally:
                fcntl.flock(file.fileno(), fcntl.LOCK_UN)

    def add_asics_to_ready_file(self, asic_ids):
        """
        Add Asic IDs to the asic ready file
        Args:
            asic_ids (list): list of asic ids to add (numbers)
        """
        with open(ASIC_READY_CONTAINER_FILE, 'r') as file:
            fcntl.flock(file.fileno(), fcntl.LOCK_SH)
            try:
                existing = {line.strip() for line in file}
            finally:
                fcntl.flock(file.fileno(), fcntl.LOCK_UN)

        with open(ASIC_READY_CONTAINER_FILE, 'a') as file:
            fcntl.flock(file.fileno(), fcntl.LOCK_EX)
            try:
                for asic_id in asic_ids:
                    entry = f"asic{asic_id}"
                    if entry not in existing:
                        file.write(entry +"\n")
            finally:
                fcntl.flock(file.fileno(), fcntl.LOCK_UN)

    def wait_module_ready(self):
        """Wait up to MODULE_READY_MAX_WAIT_TIME seconds for all modules to be ready
        """
        if utils.is_host():
            module_ready_file = MODULE_READY_HOST_FILE
        else:
            module_ready_file = MODULE_READY_CONTAINER_FILE

        if os.path.exists(module_ready_file):
            self.initialized = True
            return
        else:
            print('Waiting module to be initialized...')
        
        if utils.wait_until(os.path.exists, MODULE_READY_MAX_WAIT_TIME, MODULE_READY_CHECK_INTERVAL, module_ready_file):
            self.initialized = True
        else:
            logger.log_error('Module initialization timeout', True)
            
    def is_initialization_owner(self):
        """Indicate whether current thread is the owner of doing module initialization

        Returns:
            bool: True if current thread is the owner
        """
        cmd = os.path.basename(sys.argv[0])
        return DEDICATE_INIT_DAEMON in cmd
    
    def set_asic_ready_value(self, asic_id, ready_bool):
        """
        Update self.initialized_list with ready_bool in case something has changed.
        """
        self.initialized_list[asic_id] = ready_bool
        if not ready_bool:
            # if the asic becomes not ready, remove it from the ready file
            self.remove_asics_from_ready_file([asic_id])

@atexit.register
def clean_up():
    """Remove module ready file when program exits.
    When module host management is enabled, xcvrd is the dependency for all other
    daemon/CLI who potentially uses SFP API.
    """
    if initialization_owner:
        ModuleHostMgmtInitializer.remove_module_ready_file()
