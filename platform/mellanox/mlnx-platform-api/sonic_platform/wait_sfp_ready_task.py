#
# SPDX-FileCopyrightText: NVIDIA CORPORATION & AFFILIATES
# Copyright (c) 2024-2025 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
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

from typing import Any


import copy
import threading
import time
from sonic_py_common.logger import Logger

logger = Logger()
EMPTY_SET = set()


JOB_STATUS_WAITING = 1
JOB_STATUS_DONE = 2
JOB_STATUS_TIMEOUT = 3

class WaitSfpReadyJob:
    FIRMWARE_LOAD_TIME = 3
    EEPROM_READY_TIME = 5

    def __init__(self, sfp_object):
        self.sfp_object = sfp_object
        now = time.monotonic()
        self.firmware_ready_time = now + self.FIRMWARE_LOAD_TIME
        self.eeprom_ready_time = self.firmware_ready_time + self.EEPROM_READY_TIME
        self.status = JOB_STATUS_WAITING
        self.error = None
        logger.log_debug(f'SFP {sfp_object.sdk_index} is scheduled for waiting reset done')
        
    def check_done(self, now):
        if now <= self.firmware_ready_time:
            self.status = JOB_STATUS_WAITING
        else:
            if now > self.eeprom_ready_time:
                self.status = JOB_STATUS_TIMEOUT
            else:
                ready, self.error = self.sfp_object.is_sfp_ready()
                self.status = JOB_STATUS_DONE if ready else JOB_STATUS_WAITING
        return self.status
                
    def get_sfp_index(self):
        return self.sfp_object.sdk_index


class WaitSfpReadyTask(threading.Thread):
    """When bring a module from powered off to powered on, it takes 3 seconds
    for module to load its firmware. This class is designed to perform a wait for
    those modules who are loading firmware.
    """
    WAIT_TIME = 3
    
    def __init__(self):
        # Set daemon to True so that the thread will be destroyed when daemon exits.
        super().__init__(daemon=True)
        self.running = False
        
        # Lock to protect the wait list 
        self.lock = threading.Lock()
        
        # Event to wake up thread function
        self.event = threading.Event()
        
        # A list of SFP to be waited. Key is SFP index, value is the expire time.
        self._wait_dict = {}
        
        # The queue to store those SFPs who finish waiting SFP ready
        self._ready_set = set()
        
        # The queue to store those SFPs who failed to wait SFP ready
        self._fail_set = set()
        
    def stop(self):
        """Stop the task, only used in unit test
        """
        self.running = False
        self.event.set()
        
    def schedule_wait(self, job):
        """Add a SFP to the wait list

        Args:
            sfp_index (int): the index of the SFP object
        """
        
        with self.lock:
            is_empty = len(self._wait_dict) == 0
  
            # The item will be expired in 3 seconds
            self._wait_dict[job.get_sfp_index()] = job

        if is_empty:
            logger.log_debug('An item arrives, wake up WaitSfpReadyTask')
            # wake up the thread
            self.event.set()
    
    def cancel_wait(self, sfp_index):
        """Cancel a SFP from the wait list

        Args:
            sfp_index (int): the index of the SFP object
        """
        logger.log_debug(f'SFP {sfp_index} is canceled job')
        with self.lock:
            if sfp_index in self._wait_dict:
                self._wait_dict.pop(sfp_index)
            if sfp_index in self._ready_set:
                self._ready_set.pop(sfp_index)
            if sfp_index in self._fail_set:
                self._fail_set.pop(sfp_index)
                
    def get_finished_set(self):
        """Get ready set and clear it

        Returns:
            set: a deep copy of self._ready_set
        """
        with self.lock:
            if not self._ready_set:
                ready_set = EMPTY_SET
            else:
                ready_set = copy.deepcopy(self._ready_set)
            if not self._fail_set:
                fail_set = EMPTY_SET
            else:
                fail_set = copy.deepcopy(self._fail_set)
            self._ready_set.clear()
            self._fail_set.clear()
        return ready_set, fail_set
            
    def empty(self):
        """Indicate if wait_dict is empty

        Returns:
            bool: True if wait_dict is empty
        """
        with self.lock:
            return len(self._wait_dict) == 0

    def run(self):
        """Thread function
        """
        self.running = True
        pending_remove_set = set()
        is_empty = True
        while self.running:
            if is_empty:
                logger.log_debug(f'WaitSfpReadyTask is waiting for task...')
                # If wait_dict is empty, hold the thread until an item coming
                self.event.wait()
                self.event.clear()

            now = time.monotonic()
            with self.lock:
                logger.log_debug(f'Processing wait SFP dict: {self._wait_dict}, now={now}')
                for sfp_index, job in self._wait_dict.items():
                    job_status = job.check_done(now)
                    if job_status == JOB_STATUS_TIMEOUT:
                        pending_remove_set.add(sfp_index)
                        self._fail_set.add(sfp_index)
                        logger.log_error(f'SFP {sfp_index} failed to wait SFP ready: {job.error}')
                    elif job_status == JOB_STATUS_DONE:
                        pending_remove_set.add(sfp_index)
                        self._ready_set.add(sfp_index)

                for sfp_index in pending_remove_set:
                    self._wait_dict.pop(sfp_index)
                    
                is_empty = (len(self._wait_dict) == 0)
                    
            pending_remove_set.clear()
            time.sleep(1)
