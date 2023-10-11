import threading
import time
import queue
import os
import select
import traceback

try:
    from sonic_py_common.logger import Logger
    from sonic_py_common import device_info, multi_asic
    from .device_data import DeviceDataManager
    from sonic_platform_base.sfp_base import SfpBase
    from sonic_platform_base.sonic_xcvr.fields import consts
    from . import sfp as sfp_module
    from . import utils
    from swsscommon.swsscommon import SonicV2Connector
except ImportError as e:
    raise ImportError (str(e) + "- required module not found")

# Global logger class instance
logger = Logger()

STATE_HW_NOT_PRESENT = "Initial state. module is not plugged to cage."
STATE_HW_PRESENT = "Module is plugged to cage"
STATE_MODULE_AVAILABLE = "Module hw present and power is good"
STATE_POWERED = "Module power is already loaded"
STATE_NOT_POWERED = "Module power is not loaded"
STATE_FW_CONTROL = "The module is not CMIS and FW needs to handle"
STATE_SW_CONTROL = "The module is CMIS and SW needs to handle"
STATE_ERROR_HANDLER = "An error occurred - read/write error, power limit or power cap."
STATE_POWER_LIMIT_ERROR = "The cage has not enough power for the plugged module"
STATE_SYSFS_ERROR = "An error occurred while writing/reading SySFS."

INDEP_PROFILE_FILE = "/{}/independent_mode_support.profile"
SAI_INDEP_MODULE_MODE = "SAI_INDEPENDENT_MODULE_MODE"
SAI_INDEP_MODULE_MODE_DELIMITER = "="
SAI_INDEP_MODULE_MODE_TRUE_STR = "1"
SYSFS_LEGACY_FD_PRESENCE = "/sys/module/sx_core/asic0/module{}/present"
ASIC_NUM = 0
PORT_BREAKOUT = 8
SYSFS_INDEPENDENT_FD_PREFIX_WO_MODULE = "/sys/module/sx_core/asic{}".format(ASIC_NUM)
SYSFS_INDEPENDENT_FD_PREFIX = SYSFS_INDEPENDENT_FD_PREFIX_WO_MODULE + "/module{}"
SYSFS_INDEPENDENT_FD_PRESENCE = '/'.join([SYSFS_INDEPENDENT_FD_PREFIX, "hw_present"])
SYSFS_INDEPENDENT_FD_POWER_GOOD = '/'.join([SYSFS_INDEPENDENT_FD_PREFIX, "power_good"])
SYSFS_INDEPENDENT_FD_POWER_ON = '/'.join([SYSFS_INDEPENDENT_FD_PREFIX, "power_on"])
SYSFS_INDEPENDENT_FD_HW_RESET = '/'.join([SYSFS_INDEPENDENT_FD_PREFIX, "hw_reset"])
SYSFS_INDEPENDENT_FD_POWER_LIMIT = '/'.join([SYSFS_INDEPENDENT_FD_PREFIX, "power_limit"])
SYSFS_INDEPENDENT_FD_FW_CONTROL = '/'.join([SYSFS_INDEPENDENT_FD_PREFIX, "control"])
# echo <val>  /sys/module/sx_core/$asic/$module/frequency   //  val: 0 - up to 400KHz, 1 - up to 1MHz
SYSFS_INDEPENDENT_FD_FREQ = '/'.join([SYSFS_INDEPENDENT_FD_PREFIX, "frequency"])
IS_INDEPENDENT_MODULE = 'is_independent_module'

STATE_DB_TABLE_NAME_PREFIX = 'TRANSCEIVER_MODULES_MGMT|{}'

MAX_EEPROM_ERROR_RESET_RETRIES = 4

class ModulesMgmtTask(threading.Thread):
    RETRY_EEPROM_READING_INTERVAL = 60

    def __init__(self, namespaces=None, main_thread_stop_event=None, q=None, l=None):
        threading.Thread.__init__(self)
        self.name = "ModulesMgmtTask"
        self.main_thread_stop_event = main_thread_stop_event
        self.sfp_error_dict = {}
        self.sfp_insert_events = {}
        self.sfp_port_dict_initial = {}
        self.sfp_port_dict = {}
        self.sfp_changes_dict = {}
        self.sfp_delete_list_from_port_dict = []
        self.namespaces = namespaces
        self.modules_changes_queue = q
        self.modules_queue_lock = l
        self.is_supported_indep_mods_system = False
        self.modules_lock_list = []
        # A set to hold those modules waiting 3 seconds since power on and hw reset
        self.waiting_modules_list = set()
        self.timer = threading.Thread()
        self.timer_queue = queue.Queue()
        self.timer_queue_lock = threading.Lock()
        self.poll_obj = None
        self.fds_mapping_to_obj = {}
        self.fds_events_count_dict = {}
        self.delete_ports_from_state_db_list = []

    # SFPs state machine
    def get_sm_func(self, sm, port):
        SFP_SM_ENUM = {STATE_HW_NOT_PRESENT: self.check_if_hw_present
            , STATE_HW_PRESENT: self.checkIfModuleAvailable
            , STATE_MODULE_AVAILABLE: self.checkIfPowerOn
            , STATE_NOT_POWERED: self.powerOnModule
            , STATE_POWERED: self.checkModuleType
            , STATE_FW_CONTROL: self.saveModuleControlMode
            , STATE_SW_CONTROL: self.saveModuleControlMode
            , STATE_ERROR_HANDLER: STATE_ERROR_HANDLER
            , STATE_POWER_LIMIT_ERROR: STATE_POWER_LIMIT_ERROR
            , STATE_SYSFS_ERROR: STATE_SYSFS_ERROR
        }
        logger.log_info("getting func for state {} for port {}".format(sm, port))
        try:
            func = SFP_SM_ENUM[sm]
            logger.log_info("got func {} for state {} for port {}".format(func, sm, port))
            return func
        except KeyError as e:
            logger.log_info("exception {} for port {}".format(e, port))
        return None

    def run(self):
        # check first if the system supports independent mode and set boolean accordingly
        (platform_path, hwsku_dir) = device_info.get_paths_to_platform_and_hwsku_dirs()
        #hwsku = device_info.get_hwsku()
        independent_file = INDEP_PROFILE_FILE.format(hwsku_dir)
        if os.path.isfile(independent_file):
            logger.log_info("file {} found, checking content for independent mode value".format(independent_file))
            with open(independent_file, "r") as independent_file_fd:
                independent_file_content = independent_file_fd.read()
                if SAI_INDEP_MODULE_MODE in independent_file_content and \
                        SAI_INDEP_MODULE_MODE_DELIMITER in independent_file_content:
                    independent_file_splitted = independent_file_content.split(SAI_INDEP_MODULE_MODE_DELIMITER)
                    if (len(independent_file_splitted) > 1):
                        self.is_supported_indep_mods_system = int(independent_file_splitted[1]) == int(SAI_INDEP_MODULE_MODE_TRUE_STR)
                        logger.log_info("file {} found, system will work in independent mode".format(independent_file))
                        logger.log_info("value of indep mode var: {} found in file".format(independent_file_splitted[1]))
        else:
            logger.log_info("file {} not found, system stays in legacy mode".format(independent_file))

        # static init - at first go over all ports and check each one if it's independent module or legacy
        self.sfp_changes_dict = {}
        # check for each port if the module connected and if it supports independent mode or legacy
        num_of_ports = DeviceDataManager.get_sfp_count()
        # create the modules sysfs fds poller
        self.poll_obj = select.poll()
        #self.poll_obj = []
        for port in range(num_of_ports):
            #temp_port_dict = {IS_INDEPENDENT_MODULE: False}
            # check sysfs per port whether it's independent mode or legacy
            temp_module_sm = ModuleStateMachine(port_num=port, initial_state=STATE_HW_NOT_PRESENT
                                              , current_state=STATE_HW_NOT_PRESENT)
            module_fd_indep_path = SYSFS_INDEPENDENT_FD_PRESENCE.format(port)
            logger.log_info("system in indep mode: {} port {}".format(self.is_supported_indep_mods_system, port))
            if self.is_supported_indep_mods_system and os.path.isfile(module_fd_indep_path):
                logger.log_info("system in indep mode: {} port {} reading file {}".format(self.is_supported_indep_mods_system, port, module_fd_indep_path))
                temp_module_sm.set_is_indep_modules(True)
                temp_module_sm.set_module_fd_path(module_fd_indep_path)
                module_fd = open(module_fd_indep_path, "r")
                temp_module_sm.set_module_fd(module_fd)
            else:
                module_fd_legacy_path = self.get_sysfs_legacy_ethernet_port_fd(SYSFS_LEGACY_FD_PRESENCE, port)
                temp_module_sm.set_module_fd_path(module_fd_legacy_path)
                module_fd = open(module_fd_legacy_path, "r")
                temp_module_sm.set_module_fd(module_fd)
            # add lock to use with timer task updating next state per module object
            self.modules_lock_list.append(threading.Lock())
            temp_module_sm.set_poll_obj(self.poll_obj)
            # start SM for this independent module
            logger.log_info("adding temp_module_sm {} to sfp_port_dict".format(temp_module_sm))
            self.sfp_port_dict_initial[port] = temp_module_sm
            self.sfp_port_dict[port] = temp_module_sm

        i = 0
        # need at least 1 module in final state until it makes sense to send changes dict
        is_final_state_module = False
        all_static_detection_done = False
        logger.log_info("sfp_port_dict before starting static detection: {}".format(self.sfp_port_dict))
        # static detection - loop on different state for all ports until all done
        while not self.main_thread_stop_event and not all_static_detection_done:
            logger.log_info("static detection running iteration {}".format(i))
            waiting_list_len = len(self.waiting_modules_list)
            sfp_port_dict_keys_len = len(self.sfp_port_dict.keys())
            if waiting_list_len == sfp_port_dict_keys_len:
                logger.log_info("static detection length of waiting list {}: {} and sfp port dict keys {}:{} is the same, sleeping 1 second..."
                              .format(waiting_list_len, self.waiting_modules_list, sfp_port_dict_keys_len, self.sfp_port_dict.keys()))
                time.sleep(1)
            else:
                logger.log_info("static detectionlength of waiting list {}: {} and sfp port dict keys {}: {} is different, NOT sleeping 1 second"
                              .format(waiting_list_len, self.waiting_modules_list, sfp_port_dict_keys_len, self.sfp_port_dict.keys()))
            for port_num, module_sm_obj in self.sfp_port_dict.items():
                curr_state = module_sm_obj.get_current_state()
                logger.log_info(f'static detection STATE_LOG {port_num}: curr_state is {curr_state}')
                func = self.get_sm_func(curr_state, port_num)
                logger.log_info("static detection got returned func {} for state {}".format(func, curr_state))
                try:
                    if not isinstance(func, str):
                        next_state = func(port_num, module_sm_obj)
                except TypeError as e:
                    logger.log_info("static detection exception {} for port {} traceback:\n{}".format(e, port_num, traceback.format_exc()))
                    module_sm_obj.set_final_state(STATE_ERROR_HANDLER)
                    continue
                logger.log_info(f'static detection STATE_LOG {port_num}: next_state is {next_state}')
                if self.timer.is_alive():
                    logger.log_info("static detection timer threads is alive, acquiring lock")
                    self.modules_lock_list[port_num].acquire()
                # for STATE_NOT_POWERED we dont advance to next state, timerTask is doing it into STATE_POWERED
                if curr_state != STATE_NOT_POWERED or not module_sm_obj.wait_for_power_on:
                    module_sm_obj.set_next_state(next_state)
                    module_sm_obj.advance_state()
                if module_sm_obj.get_final_state():
                    logger.log_info(f'static detection STATE_LOG {port_num}: enter final state {module_sm_obj.get_final_state()}')
                    is_final_state_module = True
                if self.timer.is_alive():
                    self.modules_lock_list[port_num].release()
                is_timer_alive = self.timer.is_alive()
                logger.log_info("static detection timer thread is_alive {} port {}".format(is_timer_alive, port_num))
                if STATE_NOT_POWERED == curr_state:
                    if not is_timer_alive:
                        logger.log_info ("static detection curr_state is {} and timer thread is_alive {}, running timer task thread"
                               .format(curr_state, is_timer_alive))
                        # call timer task
                        self.timer = threading.Timer(1.0, self.timerTask)
                        self.timer.start()
                    self.timer_queue.put(module_sm_obj)
                    if self.timer.is_alive():
                        logger.log_info("timer thread is_alive {}, locking module obj".format(self.timer.is_alive()))
                        self.modules_lock_list[port_num].acquire()
                    module_sm_obj.set_next_state(next_state)
                    if self.timer.is_alive():
                        logger.log_info("timer thread is_alive {}, releasing module obj".format(self.timer.is_alive()))
                        self.modules_lock_list[port_num].release()

            if is_final_state_module:
                self.add_ports_state_to_state_db()
                self.delete_ports_from_dict()
                self.send_changes_to_shared_queue()
            i += 1
            logger.log_info("sfp_port_dict: {}".format(self.sfp_port_dict))
            for port_num, module_sm_obj in self.sfp_port_dict.items():
                logger.log_info("static detection port_num: {} initial state: {} current_state: {} next_state: {}"
                       .format(port_num, module_sm_obj.initial_state, module_sm_obj.get_current_state()
                               , module_sm_obj.get_next_state()))
            sfp_port_dict_keys_len = len(self.sfp_port_dict.keys())
            if sfp_port_dict_keys_len == 0:
                logger.log_info("static detection len of keys of sfp_port_dict is 0: {}".format(sfp_port_dict_keys_len))
                all_static_detection_done = True
            else:
                logger.log_info("static detection len of keys of sfp_port_dict is not 0: {}".format(sfp_port_dict_keys_len))
            logger.log_info("static detection all_static_detection_done: {}".format(all_static_detection_done))

        logger.log_info("sfp_port_dict before dynamic detection: {}".format(self.sfp_port_dict))
        # dynamic detection - loop on polling changes, run state machine for them and put them into shared queue
        i = 0
        # need at least 1 module in final state until it makes sense to send changes dict
        is_final_state_module = False
        # initialize fds events count to 0
        for fd_fileno in self.fds_mapping_to_obj:
            module_obj = self.fds_mapping_to_obj[fd_fileno]
            # for debug purposes
            self.fds_events_count_dict[module_obj.port_num] = { 'presence' : 0 , 'power_good' : 0 }
        dummy_read = False
        while not self.main_thread_stop_event:
            logger.log_info("dynamic detection running iteration {}".format(i))
            # dummy read all sysfs fds before polling them due to linux kernel implementation of poll
            if not dummy_read:
                for fd_fileno in self.fds_mapping_to_obj:
                    # dummy read present / hw_present / power_good sysfs
                    module_obj = self.fds_mapping_to_obj[fd_fileno]['module_obj']
                    module_fd = self.fds_mapping_to_obj[fd_fileno]['fd']
                    fd_name = self.fds_mapping_to_obj[fd_fileno]['fd_name']
                    if fd_name in ['presence']:
                        module_fd_path = module_obj.module_fd_path
                    elif fd_name in ['power_good']:
                        module_fd_path = module_obj.module_power_good_fd_path
                    try:
                        logger.log_info("dynamic detection dummy reading from fd path {} for port {}"
                                      .format(module_fd_path, module_obj.port_num))
                        val = module_fd.read()
                        module_fd.seek(0)
                        val_int = None
                        if len(val) > 0:
                            val_int = int(val)
                        logger.log_info("dynamic detection dummy read presence {} int {} for port {} before polling"
                                      .format(val, val_int, module_obj.port_num))
                    except Exception as e:
                        logger.log_info("dynamic detection exception on dummy read presence {} for port {} traceback:\n{}"
                                      .format(e, module_obj.port_num, traceback.format_exc()))
                dummy_read = True
            # poll for changes with 1 second timeout
            fds_events = self.poll_obj.poll(1000)
            logger.log_info("dynamic detection polled obj checking fds_events iteration {}".format(i))
            for fd, event in fds_events:
                # get modules object from fd according to saved key-value of fd-module obj saved earlier
                logger.log_info("dynamic detection working on fd {} event {}".format(fd, event))
                #module_obj = self.fds_mapping_to_obj[fd]
                module_obj = self.fds_mapping_to_obj[fd_fileno]['module_obj']
                module_fd = self.fds_mapping_to_obj[fd_fileno]['fd']
                fd_name = self.fds_mapping_to_obj[fd_fileno]['fd_name']
                if fd_name in ['presence']:
                    module_fd_path = module_obj.module_fd_path
                elif fd_name in ['power_good']:
                    module_fd_path = module_obj.module_power_good_fd_path
                self.fds_events_count_dict[module_obj.port_num][fd_name] += 1
                val = module_fd.read()
                module_fd.seek(0)
                logger.log_info("dynamic detection got module_obj {} with port {} from fd number {} path {} count {}"
                              .format(module_obj, module_obj.port_num, fd, module_fd_path, self.fds_events_count_dict[module_obj.port_num]))
                if module_obj.port_num not in self.sfp_port_dict.keys():
                    logger.log_info("dynamic detection port {} not found in sfp_port_dict keys: {} resetting all states".format(module_obj.port_num, self.sfp_port_dict.keys()))
                    module_obj.reset_all_states()
                    # put again module obj in sfp_port_dict so next loop will work on it
                    self.sfp_port_dict[module_obj.port_num] = module_obj
                    self.delete_ports_from_state_db_list.append(module_obj.port_num)
            self.delete_ports_state_from_state_db(self.delete_ports_from_state_db_list)
            logger.log_info("dynamic detection sleeping 1 second...")
            time.sleep(1)
            for port_num, module_sm_obj in self.sfp_port_dict.items():
                curr_state = module_sm_obj.get_current_state()
                logger.log_info(f'dynamic detection STATE_LOG {port_num}: curr_state is {curr_state}')
                func = self.get_sm_func(curr_state, port)
                logger.log_info("dynamic detection got returned func {} for state {}".format(func, curr_state))
                try:
                    next_state = func(port_num, module_sm_obj, dynamic=True)
                except TypeError as e:
                    logger.log_info("exception {} for port {}".format(e, port_num))
                    continue
                logger.log_info(f'dynamic detection STATE_LOG {port_num}: next_state is {next_state}')
                if self.timer.is_alive():
                    logger.log_info("dynamic detection timer threads is alive, acquiring lock")
                    self.modules_lock_list[port_num].acquire()
                if curr_state != STATE_NOT_POWERED or not module_sm_obj.wait_for_power_on:
                    module_sm_obj.set_next_state(next_state)
                    module_sm_obj.advance_state()
                if module_sm_obj.get_final_state():
                    logger.log_info(f'dynamic detection STATE_LOG {port_num}: enter final state {module_sm_obj.get_final_state()}')
                    is_final_state_module = True
                if self.timer.is_alive():
                    self.modules_lock_list[port_num].release()
                is_timer_alive = self.timer.is_alive()
                logger.log_info("dynamic detection timer thread is_alive {} port {}".format(is_timer_alive, port_num))
                if STATE_NOT_POWERED == curr_state:
                    if not is_timer_alive:
                        logger.log_info("dynamic detection curr_state is {} and timer thread is_alive {}, running timer task thread"
                                      .format(curr_state, is_timer_alive))
                        # call timer task
                        self.timer = threading.Timer(1.0, self.timerTask)
                        self.timer.start()
                    self.timer_queue.put(module_sm_obj)
                    if self.timer.is_alive():
                        logger.log_info("dynamic detection timer thread is_alive {}, locking module obj".format(self.timer.is_alive()))
                        self.modules_lock_list[port_num].acquire()
                    module_sm_obj.set_next_state(next_state)
                    if self.timer.is_alive():
                        logger.log_info(
                            "dynamic detection timer thread is_alive {}, releasing module obj".format(self.timer.is_alive()))
                        self.modules_lock_list[port_num].release()

            if is_final_state_module:
                self.add_ports_state_to_state_db(dynamic=True)
                self.delete_ports_from_dict(dynamic=True)
                self.send_changes_to_shared_queue(dynamic=True)
            i += 1
            logger.log_info("sfp_port_dict: {}".format(self.sfp_port_dict))
            for port_num, module_sm_obj in self.sfp_port_dict.items():
                logger.log_info("port_num: {} module_sm_obj initial state: {} current_state: {} next_state: {}"
                       .format(port_num, module_sm_obj.initial_state, module_sm_obj.get_current_state(), module_sm_obj.get_next_state()))


    def check_if_hw_present(self, port, module_sm_obj, dynamic=False):
        if self.is_supported_indep_mods_system:
            module_fd_indep_path = SYSFS_INDEPENDENT_FD_PRESENCE.format(port)
        else:
            module_fd_indep_path = SYSFS_LEGACY_FD_PRESENCE.format(port)
        if os.path.isfile(module_fd_indep_path):
            try:
                val_int = utils.read_int_from_file(module_fd_indep_path)
                if 0 == val_int:
                    logger.log_info("returning {} for val {}".format(STATE_HW_NOT_PRESENT, val_int))
                    module_sm_obj.set_final_state(STATE_HW_NOT_PRESENT)
                    return STATE_HW_NOT_PRESENT
                elif 1 == val_int:
                    if not self.is_supported_indep_mods_system:
                        module_sm_obj.set_final_state(STATE_HW_PRESENT)
                    logger.log_info("returning {} for val {}".format(STATE_HW_PRESENT, val_int))
                    return STATE_HW_PRESENT
            except Exception as e:
                logger.log_info("exception {} for port {} setting final state STATE_ERROR_HANDLER".format(e, port))
                module_sm_obj.set_final_state(STATE_ERROR_HANDLER)
                return STATE_ERROR_HANDLER
        module_sm_obj.set_final_state(STATE_HW_NOT_PRESENT)
        return STATE_HW_NOT_PRESENT

    def checkIfModuleAvailable(self, port, module_sm_obj, dynamic=False):
        logger.log_info("enter check_if_module_available port {} module_sm_obj {}".format(port, module_sm_obj))
        module_fd_indep_path = SYSFS_INDEPENDENT_FD_POWER_GOOD.format(port)
        if os.path.isfile(module_fd_indep_path):
            try:
                # not using utils.read_int_from_file since need to catch the exception here if no such file or it is
                # not accesible. utils.read_int_from_file will return 0 in such a case
                module_power_good_fd = open(module_fd_indep_path, "r")
                val = module_power_good_fd.read()
                val_int = int(val)
                module_sm_obj.module_power_good_fd_path = module_fd_indep_path
                module_sm_obj.module_power_good_fd = module_power_good_fd
                # registering power good sysfs even if not good, so we can get an event from poller upon changes
                self.poll_obj.register(module_sm_obj.module_power_good_fd, select.POLLERR | select.POLLPRI)
                self.fds_mapping_to_obj[module_sm_obj.module_power_good_fd.fileno()] = { 'module_obj' : module_sm_obj
                                                    , 'fd':module_sm_obj.module_power_good_fd, 'fd_name' : 'power_good'}
                if 0 == val_int:
                    logger.log_info(f'port {port} power is not good')
                    module_sm_obj.set_final_state(STATE_HW_NOT_PRESENT)
                    return STATE_HW_NOT_PRESENT
                elif 1 == val_int:
                    logger.log_info(f'port {port} power is good')
                    return STATE_MODULE_AVAILABLE
            except Exception as e:
                logger.log_info("exception {} for port {}".format(e, port))
                return STATE_HW_NOT_PRESENT
        logger.log_info(f'port {port} has no power good file {module_fd_indep_path}')
        module_sm_obj.set_final_state(STATE_HW_NOT_PRESENT)
        return STATE_HW_NOT_PRESENT

    def checkIfPowerOn(self, port, module_sm_obj, dynamic=False):
        logger.log_info(f'enter checkIfPowerOn for port {port}')
        module_fd_indep_path = SYSFS_INDEPENDENT_FD_POWER_ON.format(port)
        if os.path.isfile(module_fd_indep_path):
            try:
                val = utils.read_int_from_file(module_fd_indep_path)
                val_int = int(val)
                if 0 == val_int:
                    logger.log_info(f'port {port} is not powered')
                    return STATE_NOT_POWERED
                elif 1 == val_int:
                    if not module_sm_obj.wait_for_power_on and \
                            utils.read_int_from_file(SYSFS_INDEPENDENT_FD_HW_RESET.format(port)) == 1:
                        sfp = sfp_module.SFP(port)
                        xcvr_api = sfp.get_xcvr_api()
                        # only if xcvr_api is None or if it is not active optics cables need reset
                        if not xcvr_api or xcvr_api.is_flat_memory():
                            logger.log_info(f'port {port} is powered, but need reset')
                            utils.write_file(SYSFS_INDEPENDENT_FD_HW_RESET.format(port), 0)
                            module_sm_obj.reset_start_time = time.time()
                            module_sm_obj.wait_for_power_on = True
                            utils.write_file(SYSFS_INDEPENDENT_FD_HW_RESET.format(port), 1)
                            module_sm_obj.reset_start_time = time.time()
                            module_sm_obj.wait_for_power_on = True
                            self.waiting_modules_list.add(module_sm_obj.port_num)
                            return STATE_NOT_POWERED
                    logger.log_info(f'port {port} is powered, does not need reset')
                    return STATE_POWERED
            except Exception as e:
                logger.log_info(f'got exception {e} in checkIfPowerOn')
                module_sm_obj.set_final_state(STATE_HW_NOT_PRESENT)
                return STATE_HW_NOT_PRESENT

    def powerOnModule(self, port, module_sm_obj, dynamic=False):
        #if module_sm_obj not in self.waiting_modules_list:
        if not module_sm_obj.wait_for_power_on:
            module_fd_indep_path_po = SYSFS_INDEPENDENT_FD_POWER_ON.format(port)
            module_fd_indep_path_r = SYSFS_INDEPENDENT_FD_HW_RESET.format(port)
            try:
                if os.path.isfile(module_fd_indep_path_po):
                    logger.log_info("powerOnModule powering on via {} for port {}".format(module_fd_indep_path_po, port))
                    # echo 1 > /sys/module/sx_core/$asic/$module/power_on
                    with open(module_fd_indep_path_po, "w") as module_fd:
                        module_fd.write("1")
                if os.path.isfile(module_fd_indep_path_r):
                    logger.log_info("powerOnModule resetting via {} for port {}".format(module_fd_indep_path_r, port))
                    # echo 0 > /sys/module/sx_core/$asic/$module/hw_reset
                    with open(module_fd_indep_path_r, "w") as module_fd:
                        module_fd.write("0")
                module_sm_obj.reset_start_time = time.time()
                module_sm_obj.wait_for_power_on = True
                self.waiting_modules_list.add(module_sm_obj.port_num)
            except Exception as e:
                logger.log_info("exception in powerOnModule {} for port {}".format(e, port))
                return STATE_HW_NOT_PRESENT
        return STATE_NOT_POWERED

    def checkModuleType(self, port, module_sm_obj, dynamic=False):
        logger.log_info("enter checkModuleType port {} module_sm_obj {}".format(port, module_sm_obj))
        sfp = sfp_module.SFP(port)
        xcvr_api = sfp.get_xcvr_api()
        if not xcvr_api:
            logger.log_info("checkModuleType calling sfp reinit for port {} module_sm_obj {}".format(port, module_sm_obj))
            sfp.reinit()
            logger.log_info("checkModuleType setting as FW control as xcvr_api is empty for port {} module_sm_obj {}".format(port, module_sm_obj))
            return STATE_FW_CONTROL
        field = xcvr_api.xcvr_eeprom.mem_map.get_field(consts.ID_FIELD)
        module_type_ba = xcvr_api.xcvr_eeprom.reader(field.get_offset(), field.get_size())
        if module_type_ba is None:
            logger.log_info("checkModuleType module_type is None for port {} - checking if we didnt retry yet max number of retries: {}".format(port, MAX_EEPROM_ERROR_RESET_RETRIES))
            # if we didnt do this retry yet - do it up to 3 times - workaround for FW issue blocking upper page access
            if module_sm_obj.eeprom_poweron_reset_retries < MAX_EEPROM_ERROR_RESET_RETRIES:
                logger.log_info("checkModuleType module_type is None retrying by falling back to STATE_NOT_POWERED eeprom reset retries {}"
                              " for port {}".format(module_sm_obj.eeprom_poweron_reset_retries, port))
                if module_sm_obj.eeprom_poweron_reset_retries % 2 == 0:
                    utils.write_file(SYSFS_INDEPENDENT_FD_HW_RESET.format(port), "0")
                    logger.log_info("checkModuleType sleeping 1 second...")
                    time.sleep(1)
                else:
                    utils.write_file(SYSFS_INDEPENDENT_FD_HW_RESET.format(port), "1")
                self.add_port_to_wait_reset(module_sm_obj)
                module_sm_obj.eeprom_poweron_reset_retries += 1
                return STATE_NOT_POWERED
            else:
                logger.log_info("checkModuleType module_type is None and already retried - setting as STATE_ERROR_HANDLER"
                              "for port {}".format(port))
                module_sm_obj.set_final_state(STATE_ERROR_HANDLER)
                return STATE_ERROR_HANDLER
        module_type = int.from_bytes(module_type_ba, "big")
        logger.log_info("got module_type {} in check_module_type port {} module_sm_obj {}".format(module_type, port, module_sm_obj))
        # QSFP-DD ID is 24, OSFP ID is 25 - only these 2 are supported currently as independent module - SW controlled
        if module_type not in [24, 25]:
            logger.log_info("setting STATE_FW_CONTROL for {} in check_module_type port {} module_sm_obj {}".format(module_type, port, module_sm_obj))
            module_sm_obj.set_final_state = STATE_FW_CONTROL
            return STATE_FW_CONTROL
        else:
            if xcvr_api.is_flat_memory():
                logger.log_info("check_module_type port {} setting STATE_FW_CONTROL module ID {} due to flat_mem device"
                              .format(module_type, port))
                return STATE_FW_CONTROL
            logger.log_info("checking power cap for {} in check_module_type port {} module_sm_obj {}"
                               .format(module_type, port, module_sm_obj))
            power_cap = self.checkPowerCap(port, module_sm_obj)
            if power_cap is STATE_POWER_LIMIT_ERROR:
                module_sm_obj.set_final_state(STATE_POWER_LIMIT_ERROR)
                return STATE_POWER_LIMIT_ERROR
            else:
                # read the module maximum supported clock of Management Comm Interface (MCI) from module EEPROM.
                # from byte 2 bits 3-2:
                # 00b means module supports up to 400KHz
                # 01b means module supports up to 1MHz
                logger.log_info(f"check_module_type reading mci max frequency for port {port}")
                read_mci = xcvr_api.xcvr_eeprom.read_raw(2, 1)
                logger.log_info(f"check_module_type read mci max frequency {read_mci} for port {port}")
                mci_bits = read_mci & 0b00001100
                logger.log_info(f"check_module_type read mci max frequency bits {mci_bits} for port {port}")
                # Then, set it to frequency Sysfs using:
                # echo <val> > /sys/module/sx_core/$asic/$module/frequency //  val: 0 - up to 400KHz, 1 - up to 1MHz
                indep_fd_freq = SYSFS_INDEPENDENT_FD_FREQ.format(port)
                utils.write_file(indep_fd_freq, mci_bits)
                return STATE_SW_CONTROL

    def checkPowerCap(self, port, module_sm_obj, dynamic=False):
        logger.log_info("enter checkPowerCap port {} module_sm_obj {}".format(port, module_sm_obj))
        #sfp_base_module = SfpBase()
        sfp = sfp_module.SFP(port)
        xcvr_api = sfp.get_xcvr_api()
        field = xcvr_api.xcvr_eeprom.mem_map.get_field(consts.MAX_POWER_FIELD)
        powercap_ba = xcvr_api.xcvr_eeprom.reader(field.get_offset(), field.get_size())
        logger.log_info("checkPowerCap got powercap bytearray {} for port {} module_sm_obj {}".format(powercap_ba, port, module_sm_obj))
        powercap = int.from_bytes(powercap_ba, "big")
        logger.log_info("checkPowerCap got powercap {} for port {} module_sm_obj {}".format(powercap, port, module_sm_obj))
        indep_fd_power_limit = self.get_sysfs_ethernet_port_fd(SYSFS_INDEPENDENT_FD_POWER_LIMIT, port)
        #with open(indep_fd_power_limit, "r") as power_limit_fd:
        #    cage_power_limit = power_limit_fd.read()
        cage_power_limit = utils.read_int_from_file(indep_fd_power_limit)
        logger.log_info("checkPowerCap got cage_power_limit {} for port {} module_sm_obj {}".format(cage_power_limit, port, module_sm_obj))
        if powercap > int(cage_power_limit):
            logger.log_info("checkPowerCap powercap {} != cage_power_limit {} for port {} module_sm_obj {}".format(powercap, cage_power_limit, port, module_sm_obj))
            module_sm_obj.set_final_state(STATE_POWER_LIMIT_ERROR)
            return STATE_POWER_LIMIT_ERROR

    def saveModuleControlMode(self, port, module_sm_obj, dynamic=False):
        logger.log_info("saveModuleControlMode setting current state {} for port {} as final state".format(module_sm_obj.get_current_state(), port))
        # bug - need to find root cause and fix
        #module_sm_obj.set_final_state(module_sm_obj.get_current_state())
        state = module_sm_obj.get_current_state()
        module_sm_obj.final_state = state
        if state == STATE_FW_CONTROL:
            #"echo 0 > /sys/module/sx_core/$asic/$module/control"
            indep_fd_fw_control = SYSFS_INDEPENDENT_FD_FW_CONTROL.format(port)
            with open(indep_fd_fw_control, "w") as fw_control_fd:
                fw_control_fd.write("0")
            logger.log_info("saveModuleControlMode set FW control for state {} port {}".format(state, port))
            module_fd_legacy_path = SYSFS_LEGACY_FD_PRESENCE.format(port)
            module_sm_obj.set_module_fd_path(module_fd_legacy_path)
            module_fd = open(module_fd_legacy_path, "r")
            module_sm_obj.set_module_fd(module_fd)
            logger.log_info("saveModuleControlMode changed module fd to legacy present for port {}".format(port))
        # register the module's sysfs fd to poller with ERR and PRI attrs
        logger.log_info("saveModuleControlMode registering sysfs fd {} number {} path {} for port {}"
                      .format(module_sm_obj.module_fd, module_sm_obj.module_fd.fileno(), module_sm_obj.set_module_fd_path, port))
        self.poll_obj.register(module_sm_obj.module_fd, select.POLLERR | select.POLLPRI)
        self.fds_mapping_to_obj[module_sm_obj.module_fd.fileno()] = { 'module_obj' : module_sm_obj
                                                    , 'fd': module_sm_obj.module_fd, 'fd_name' : 'presence' }
        module_sm_obj.set_poll_obj(self.poll_obj)
        logger.log_info("saveModuleControlMode set current state {} for port {} as final state {}".format(
            module_sm_obj.get_current_state(), port, module_sm_obj.get_final_state()))

    def timerTask(self): # wakes up every 1 second
        logger.log_info("timerTask entered run state")
        empty = False
        i = 0
        while not empty:
            logger.log_info("timerTask while loop itartion {}".format(i))
            empty = True
            port_list_to_delete = []
            for port in self.waiting_modules_list:
                logger.log_info("timerTask working on port {}".format(port))
                empty = False
                module = self.sfp_port_dict[port]
                logger.log_info("timerTask got module with port_num {} from port {}".format(module.port_num, port))
                state = module.get_current_state()
                if module and state == STATE_NOT_POWERED:
                    logger.log_info("timerTask module {} current_state {} counting seconds since reset_start_time"
                                  .format(module, module.get_current_state()))
                    if time.time() - module.reset_start_time >= 3:
                        # set next state as STATE_POWERED state to trigger the function of check module type
                        logger.log_info("timerTask module port {} locking lock of port {}".format(module.port_num, module.port_num))
                        self.modules_lock_list[module.port_num].acquire()
                        logger.log_info("timerTask module port {} setting next state to STATE_POWERED".format(module.port_num))
                        module.set_next_state(STATE_POWERED)
                        logger.log_info("timerTask module port {} advancing next state".format(module.port_num))
                        module.advance_state()
                        logger.log_info("timerTask module port {} releasing lock of port {}".format(port, module.port_num))
                        self.modules_lock_list[module.port_num].release()
                        logger.log_info("timerTask module port {} adding to delete list to remove from waiting_modules_list".format(module.port_num))
                        port_list_to_delete.append(module.port_num)
            logger.log_info("timerTask deleting ports {} from waiting_modules_list...".format(port_list_to_delete))
            for port in port_list_to_delete:            
                logger.log_info("timerTask deleting port {} from waiting_modules_list".format(port))
                self.waiting_modules_list.remove(port)
            logger.log_info("timerTask waiting_modules_list after deletion: {}".format(self.waiting_modules_list))
            time.sleep(1)
            i += 1
    def get_sysfs_legacy_ethernet_port_fd(self, sysfs_fd, port):
        breakout_port = "Ethernet{}".format(port * PORT_BREAKOUT)
        sysfs_eth_port_fd = sysfs_fd.format(breakout_port)
        return sysfs_eth_port_fd

    def get_sysfs_ethernet_port_fd(self, sysfs_fd, port):
        sysfs_eth_port_fd = sysfs_fd.format(port)
        return sysfs_eth_port_fd

    def add_port_to_wait_reset(self, module_sm_obj):
        module_sm_obj.reset_start_time = time.time()
        logger.log_info("add_port_to_wait_reset reset_start_time {}".format(module_sm_obj.reset_start_time))
        module_sm_obj.wait_for_power_on = True
        logger.log_info("add_port_to_wait_reset wait_for_power_on {}".format(module_sm_obj.wait_for_power_on))
        self.waiting_modules_list.add(module_sm_obj.port_num)
        logger.log_info("add_port_to_wait_reset waiting_list after adding: {}".format(self.waiting_modules_list))

    def add_ports_state_to_state_db(self, dynamic=False):
        state_db = None
        detection_method = 'dynamic' if dynamic else 'static'
        for port, module_obj in self.sfp_port_dict.items():
            final_state = module_obj.get_final_state()
            if final_state:
                # add port to delete list that we will iterate on later and delete the ports from sfp_port_dict
                self.sfp_delete_list_from_port_dict.append(port)
                if final_state in [STATE_HW_NOT_PRESENT, STATE_POWER_LIMIT_ERROR, STATE_ERROR_HANDLER]:
                    ctrl_type_db_value = '0'
                else:
                    ctrl_type_db_value = '1'
                self.sfp_changes_dict[str(module_obj.port_num)] = ctrl_type_db_value
                if final_state in [STATE_SW_CONTROL, STATE_FW_CONTROL]:
                    namespaces = multi_asic.get_front_end_namespaces()
                    for namespace in namespaces:
                        logger.log_info(f"{detection_method} detection getting state_db for port {port} namespace {namespace}")
                        state_db = SonicV2Connector(use_unix_socket_path=False, namespace=namespace)
                        logger.log_info(f"{detection_method} detection getting state_db for port {port} namespace {namespace}")
                        logger.log_info(f"{detection_method} detection got state_db for port {port} namespace {namespace}")
                        if state_db is not None:
                            logger.log_info(
                                f"{detection_method} detection connecting to state_db for port {port} namespace {namespace}")
                            state_db.connect(state_db.STATE_DB)
                            if final_state in [STATE_FW_CONTROL]:
                                control_type = 'FW_CONTROL'
                            elif final_state in [STATE_SW_CONTROL]:
                                control_type = 'SW_CONTROL'
                            table_name = STATE_DB_TABLE_NAME_PREFIX.format(port)
                            logger.log_info(f"{detection_method} detection setting state_db table {table_name} for port {port}"
                                               f" namespace {namespace} control_type {control_type}")
                            state_db.set(state_db.STATE_DB, table_name, "control_type", control_type)

    def delete_ports_state_from_state_db(self, ports, dynamic=True):
        state_db = None
        detection_method = 'dynamic' if dynamic else 'static'
        for port in ports:
            namespaces = multi_asic.get_front_end_namespaces()
            for namespace in namespaces:
                logger.log_info(f"{detection_method} detection getting state_db for port {port} namespace {namespace}")
                state_db = SonicV2Connector(use_unix_socket_path=False, namespace=namespace)
                logger.log_info(f"{detection_method} detection got state_db for port {port} namespace {namespace}")
                if state_db is not None:
                    logger.log_info(
                        f"{detection_method} detection connecting to state_db for port {port} namespace {namespace}")
                    state_db.connect(state_db.STATE_DB)
                    table_name = STATE_DB_TABLE_NAME_PREFIX.format(port)
                    logger.log_info(f"{detection_method} detection deleting state_db table {table_name} "
                                       f"for port {port} namespace {namespace}")
                    state_db.delete(state_db.STATE_DB, table_name)

    def delete_ports_from_dict(self, dynamic=False):
        detection_method = 'dynamic' if dynamic else 'static'
        logger.log_info(f"{detection_method} detection sfp_port_dict before deletion: {self.sfp_port_dict}")
        for port in self.sfp_delete_list_from_port_dict:
            del self.sfp_port_dict[port]
        self.sfp_delete_list_from_port_dict = []
        logger.log_info("dynamic detection sfp_port_dict after deletion: {}".format(self.sfp_port_dict))

    def send_changes_to_shared_queue(self, dynamic=False):
        detection_method = 'dynamic' if dynamic else 'static'
        if self.sfp_changes_dict:
            logger.log_info(f"{detection_method} detection putting sfp_changes_dict {self.sfp_changes_dict} "
                               f"in modules changes queue...")
            try:
                self.modules_queue_lock.acquire()
                self.modules_changes_queue.put(self.sfp_changes_dict, timeout=1)
                self.modules_queue_lock.release()
                self.sfp_changes_dict = {}
            except queue.Full:
                logger.log_info(f"{detection_method} failed to put item from modules changes queue, queue is full")
        else:
            logger.log_info(f"{detection_method} sfp_changes_dict {self.sfp_changes_dict} is empty...")


class ModuleStateMachine(object):

    def __init__(self, port_num=0, initial_state=STATE_HW_NOT_PRESENT, current_state=STATE_HW_NOT_PRESENT
                 , next_state=STATE_HW_NOT_PRESENT, final_state='', is_indep_module=False
                 , module_fd_path='', module_fd=None, poll_obj=None, reset_start_time=None 
                 , eeprom_poweron_reset_retries=1):

        self.port_num = port_num
        self.initial_state = initial_state
        self.current_state = current_state
        self.next_state = next_state
        self.final_state = final_state
        self.is_indep_modules = is_indep_module
        self.module_fd_path = module_fd_path
        self.module_fd = module_fd
        self.poll_obj = poll_obj
        self.reset_start_time = reset_start_time
        self.wait_for_power_on = False
        self.eeprom_poweron_reset_retries = eeprom_poweron_reset_retries
        self.module_power_good_fd_path = module_fd_path
        self.module_power_good_fd = module_fd

    def set_initial_state(self, state):
        self.initial_state = state

    def get_current_state(self):
        return self.current_state

    def set_current_state(self, state):
        self.current_state = state

    def get_next_state(self):
        return self.next_state

    def set_next_state(self, state):
        self.next_state = state

    def get_final_state(self):
        return self.final_state

    def set_final_state(self, state):
        self.final_state = state

    def advance_state(self):
        self.set_current_state(self.next_state)
        self.next_state = ''

    def set_is_indep_modules(self, is_indep_modules):
        self.is_indep_modules = is_indep_modules

    def set_module_fd_path(self, module_fd_path):
        self.module_fd_path = module_fd_path

    def set_module_fd(self, module_fd):
        self.module_fd = module_fd

    def get_poll_obj(self):
        return self.poll_obj

    def set_poll_obj(self, poll_obj):
        self.poll_obj = poll_obj

    def reset_all_states(self, def_state=STATE_HW_NOT_PRESENT, retries=1):
        self.initial_state = def_state
        self.current_state = def_state
        self.next_state = def_state
        self.final_state = ''
        self.wait_for_power_on = False
        self.eeprom_poweron_reset_retries = retries
