# Counters Enabling Redesign

## Table of Content

### Revision

| Rev |     Date    |       Author       | Change Description                |
|:---:|:-----------:|:------------------:|-----------------------------------|
| 0.1 |  12.15.2021 | Noa Or             | Initial version                   |


## Overview

In the current design of counter enabling, when enable_counters.py script is triggered, if the switch uptime is less then 5 minutes, it sleeps for 3 minutes. This is done in order to make sure the system is up and synchronized.

The purpose of counters redesign is to change the 'sleep' mechanisem to be event driven and to have the exact time when the siwtch is ready to enable counters.

## Requirements

Change enable_counters.py script to be event driven from config_db instead of using sleeps.


## High-Level Design

## New System-up Daemon

enable_counters.py script will be refactored to be running as a daemon.

When system is up after reboot, the new daemon will be created and do the following:

- Take a dump from config DB on the expected ports state.

    The dump will be taken from PORTCHANNEL (admin_status) & PORT (admin_status) tables.

- Create an intenral data structure that will contain:

    { port: [expected_state, current_state] }
    for all ports received from config_db dump.


- Initialize an internal counter with 0.

- Start listen to events from APP DB, PORT_TABLE, LAG_MEMBER_TABLE.

    For each event arriving: //the daemon will check if the oper_status has been changed.
    - If Port table doesn't have "PortConfigDone" key, delete the event and continue to the next one.
    - If the current oper_status of the port equals to the expected, counter++.
    - If the current oper_status of the port is different from the expected, counter--.


    Eventually, when the counter will be equals to the number of ports, we will enable the counters.
    Number of ports can be taken from App_DB, PortConfigDone, count field.

    For example,
    ```
    127.0.0.1:6379> hgetall PORT_TABLE:PortConfigDone
    1) "count"
    2) "32"
    ```

The expected flow is to receive events for all of the ports that oper_status has been changed after all port config has been done.

Preliminary flow:

- portSyncd process, received the ports information from config_db, PORT table and added it to a list.
- PortConfigDone flag is being pushed to App DB.
- LinkSync goes over the list and if the port has an admin_status, it pushes state:ok to state db.
- If the port has stste:ok in state db, portmgr will start it using `ip link` command.


NOTE: The daemon will also start a timer in order to be able to enable counters even if one of the ports is not stable.
If after 3 minutes (180 seconds), the counters were not enabled yet, enable counters.


In case of link flap:
After port config was done, the daemon starts to receive events regarding the oper_status of each of the ports/LAGs.
Since the counter starts with 0, and the daemon will enale the counters when counter = number of ports, link flap will not make the daemon enable counters before all links are up.


## Action Items

- from where to take the dump? config-db after reboot (admin status) or before reboot (oper status).

- Is nedev_oper_status in STATE_DB is enough or keep uding oper_status from APP_DB?
