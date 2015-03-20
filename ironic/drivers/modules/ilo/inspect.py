# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.
"""
iLO Inspect Interface
"""
from oslo_utils import importutils
import six

from ironic.common import exception
from ironic.common.i18n import _
from ironic.common.i18n import _LI
from ironic.common.i18n import _LW
from ironic.common import states
from ironic.conductor import utils as conductor_utils
from ironic.db import api as dbapi
from ironic.drivers import base
from ironic.drivers.modules.ilo import common as ilo_common
from ironic.openstack.common import log as logging

ilo_error = importutils.try_import('proliantutils.exception')

LOG = logging.getLogger(__name__)

ESSENTIAL_PROPERTIES_KEYS = {'memory_mb', 'local_gb', 'cpus', 'cpu_arch'}
CAPABILITIES_KEYS = {'BootMode', 'secure_boot', 'rom_firmware_version',
                     'ilo_firmware_version', 'server_model', 'max_raid_level',
                     'pci_gpu_devices', 'sr_iov_devices', 'nic_capacity'}


def _create_ports_if_not_exist(node, macs):
    """Create ironic ports for the mac addresses.

    Creates ironic ports for the mac addresses returned with inspection
    or as requested by operator.

    :param node: node object.
    :param macs: A dictionary of port numbers to mac addresses
                 returned by node inspection.

    """
    node_id = node.id
    sql_dbapi = dbapi.get_instance()
    for mac in macs.values():
        port_dict = {'address': mac, 'node_id': node_id}

        try:
            sql_dbapi.create_port(port_dict)
            LOG.info(_LI("Port created for MAC address %(address)s for node "
                         "%(node)s"), {'address': mac, 'node': node.uuid})
        except exception.MACAlreadyExists:
            LOG.warn(_LW("Port already exists for MAC address %(address)s "
                         "for node %(node)s"), {'address': mac,
                         'node': node.uuid})


def _get_essential_properties(node, ilo_object):
    """Inspects the node and get essential scheduling properties

    :param node: node object.
    :param ilo_object: an instance of proliantutils.ilo.IloClient
    :raises: HardwareInspectionFailure if any of the properties values
             are missing.
    :returns: The dictionary containing properties and MAC data.
              The dictionary possible keys are 'properties' and 'macs'.
              The 'properties' should contain keys as in
              ESSENTIAL_PROPERTIES_KEYS. The 'macs' is a dictionary
              containing key:value pairs of <port_numbers:mac_addresses>

    """
    try:
        # Retrieve the mandatory properties from hardware
        result = ilo_object.get_essential_properties()
    except ilo_error.IloError as e:
        raise exception.HardwareInspectionFailure(error=e)
    _validate(node, result)
    return result


def _validate(node, data):
    """Validate the received value against the supported keys in ironic.

    :param node: node object.
    :param data: the dictionary received by querying server.
    :raises: HardwareInspectionFailure

    """
    if data.get('properties'):
        if isinstance(data['properties'], dict):
            valid_keys = ESSENTIAL_PROPERTIES_KEYS
            missing_keys = valid_keys - set(data['properties'])
            if missing_keys:
                error = (_(
                    "Server didn't return the key(s): %(key)s") %
                    {'key': ', '.join(missing_keys)})
                raise exception.HardwareInspectionFailure(error=error)
        else:
            error = (_("Essential properties are expected to be in dictionary "
                      "format, received %(properties)s from node "
                      "%(node)s.") % {"properties": data['properties'],
                                      'node': node.uuid})
            raise exception.HardwareInspectionFailure(error=error)
    else:
        error = (_("The node %s didn't return 'properties' as the key with "
                   "inspection.") % node.uuid)
        raise exception.HardwareInspectionFailure(error=error)

    if data.get('macs'):
        if not isinstance(data['macs'], dict):
            error = (_("Node %(node)s didn't return MACs %(macs)s "
                       "in dictionary format.")
                      % {"macs": data['macs'], 'node': node.uuid})
            raise exception.HardwareInspectionFailure(error=error)
    else:
        error = (_("The node %s didn't return 'macs' as the key with "
                   "inspection.") % node.uuid)
        raise exception.HardwareInspectionFailure(error=error)


def _create_supported_capabilities_dict(capabilities):
    """Creates a capabilities dictionary from supported capabilities in ironic.

    :param capabilities: a dictionary of capabilities as returned by the
                         hardware.
    :returns: a dictionary of the capabilities supported by ironic
              and returned by hardware.

    """
    valid_cap = {}
    for key in CAPABILITIES_KEYS.intersection(capabilities):
        valid_cap[key] = capabilities.get(key)
    return valid_cap


def _update_capabilities(node, new_capabilities):
    """Add or update a capability to the capabilities string.

    This method adds/updates a given property to the node capabilities
    string.
    Currently the capabilities are recorded as a string in
    properties/capabilities of a Node. It's of the below format:
    properties/capabilities='boot_mode:bios,boot_option:local'

    :param node: Node object.
    :param new_capabilities: the dictionary of capabilities returned
                             by baremetal with inspection.
    :returns: The capability string after adding/updating the
              node_capabilities with new_capabilities
    :raises: InvalidParameterValue, if node_capabilities is malformed.
    :raises: HardwareInspectionFailure, if inspected capabilities
             are not in dictionary format.

    """
    cap_dict = {}
    node_capabilities = node.properties.get('capabilities')
    if node_capabilities:
        try:
            cap_dict = dict(x.split(':', 1)
                            for x in node_capabilities.split(','))
        except ValueError:
            # Capabilities can be filled by operator.  ValueError can
            # occur in malformed capabilities like:
            # properties/capabilities='boot_mode:bios,boot_option'.
            msg = (_("Node %(node)s has invalid capabilities string "
                    "%(capabilities), unable to modify the node "
                    "properties['capabilities'] string")
                    % {'node': node.uuid, 'capabilities': node_capabilities})
            raise exception.InvalidParameterValue(msg)
    if isinstance(new_capabilities, dict):
        cap_dict.update(new_capabilities)
    else:
        msg = (_("The expected format of capabilities from inspection "
                 "is dictionary while node %(node)s returned "
                 "%(capabilities)s.") % {'node': node.uuid,
                 'capabilities': new_capabilities})
        raise exception.HardwareInspectionFailure(error=msg)
    return ','.join(['%(key)s:%(value)s' % {'key': key, 'value': value}
                     for key, value in six.iteritems(cap_dict)])


def _get_macs_for_desired_ports(node, macs):
    """Get the dict of MACs which are desired by the operator.

    Get the MACs for desired ports.
    Returns a dictionary of MACs associated with the ports specified
    in the node's driver_info/inspect_ports.

    The driver_info field is expected to be populated with
    comma-separated port numbers like driver_info/inspect_ports='1,2'.
    In this case the inspection is expected to create ironic ports
    only for these two ports.
    The proliantutils is expected to return key value pair for each
    MAC address like:
    {'Port 1': 'aa:aa:aa:aa:aa:aa', 'Port 2': 'bb:bb:bb:bb:bb:bb'}

    Possible scenarios:
    'inspect_ports' == 'all' : creates ports for all inspected MACs
    'inspect_ports' == <valid_port_numbers>: creates ports for
                                             requested port numbers.
    'inspect_ports' == <mix_of_valid_invalid> : raise error for
                                                invalid inputs.
    'inspect_ports' == 'none' : doesn't do any action with the
                                inspected mac addresses.

    This method is not called if 'inspect_ports' == 'none', hence the
    scenario is not covered under this method.

    :param node: a node object.
    :param macs: a dictionary of MAC addresses returned by the hardware
                 with inspection.
    :returns: a dictionary of port numbers and MAC addresses with only
              the MACs requested by operator in
              node.driver_info['inspect_ports']
    :raises: HardwareInspectionFailure for the non-existing ports
             requested in node.driver_info['inspect_ports']

    """
    driver_info = node.driver_info
    desired_macs = str(driver_info.get('inspect_ports'))

    # If the operator has given 'all' just return all the macs
    # returned by inspection.
    if desired_macs.lower() == 'all':
        to_be_created_macs = macs
    else:
        to_be_created_macs = {}
        # The list should look like ['Port 1', 'Port 2'] as
        # iLO returns port numbers like this.
        desired_macs_list = [
            'Port %s' % port_number
            for port_number in (desired_macs.split(','))]

        # Check if the given input is valid or not. Return all the
        # requested macs.
        non_existing_ports = []
        for port_number in desired_macs_list:
            mac_address = macs.get(port_number)
            if mac_address:
                to_be_created_macs[port_number] = mac_address
            else:
                non_existing_ports.append(port_number)

        # It is possible that operator has given a wrong input by mistake.
        if non_existing_ports:
            error = (_("Could not find requested ports %(ports)s on the "
                       "node %(node)s")
                       % {'ports': non_existing_ports, 'node': node.uuid})
            raise exception.HardwareInspectionFailure(error=error)

    return to_be_created_macs


def _get_capabilities(node, ilo_object):
    """inspects hardware and gets additional capabilities.

    :param node: Node object.
    :param ilo_object: an instance of ilo drivers.
    :returns : a string of capabilities like
               'key1:value1,key2:value2,key3:value3'
               or None.

    """
    capabilities = None
    try:
        capabilities = ilo_object.get_server_capabilities()
    except ilo_error.IloError:
        LOG.debug(("Node %s did not return any additional capabilities."),
                   node.uuid)

    return capabilities


class IloInspect(base.InspectInterface):

    def get_properties(self):
        d = ilo_common.REQUIRED_PROPERTIES.copy()
        d.update(ilo_common.INSPECT_PROPERTIES)
        return d

    def validate(self, task):
        """Check that 'driver_info' contains required ILO credentials.

        Validates whether the 'driver_info' property of the supplied
        task's node contains the required credentials information.

        :param task: a task from TaskManager.
        :raises: InvalidParameterValue if required iLO parameters
                 are not valid.
        :raises: MissingParameterValue if a required parameter is missing.
        :raises: InvalidParameterValue if invalid input provided.

        """
        node = task.node
        driver_info = ilo_common.parse_driver_info(node)
        if 'inspect_ports' not in driver_info:
            raise exception.MissingParameterValue(_(
                "Missing 'inspect_ports' parameter in node's driver_info."))
        value = driver_info['inspect_ports']
        if (value.lower() != 'all' and value.lower() != 'none'
            and not all(s.isdigit() for s in value.split(','))):
                raise exception.InvalidParameterValue(_(
                    "inspect_ports can accept either comma separated "
                    "port numbers, or a single port number, or 'all' "
                    "or 'none'. %(value)s given for node %(node)s "
                    "driver_info['inspect_ports']")
                    % {'value': value, 'node': node})

    def inspect_hardware(self, task):
        """Inspect hardware to get the hardware properties.

        Inspects hardware to get the essential and additional hardware
        properties. It fails if any of the essential properties
        are not received from the node or if 'inspect_ports' is
        not provided in driver_info.
        It doesn't fail if node fails to return any capabilities as
        the capabilities differ from hardware to hardware mostly.

        :param task: a TaskManager instance.
        :raises: HardwareInspectionFailure if essential properties
                 could not be retrieved successfully.
        :raises: IloOperationError if system fails to get power state.
        :returns: The resulting state of inspection.

        """
        power_turned_on = False
        ilo_object = ilo_common.get_ilo_object(task.node)
        try:
            state = task.driver.power.get_power_state(task)
        except exception.IloOperationError as ilo_exception:
            operation = (_("Inspecting hardware (get_power_state) on %s")
                           % task.node.uuid)
            raise exception.IloOperationError(operation=operation,
                                              error=ilo_exception)
        if state != states.POWER_ON:
            LOG.info(_LI("The node %s is not powered on. Powering on the "
                         "node for inspection."), task.node.uuid)
            conductor_utils.node_power_action(task, states.POWER_ON)
            power_turned_on = True

        # get the essential properties and update the node properties
        # with it.

        inspected_properties = {}
        result = _get_essential_properties(task.node, ilo_object)
        properties = result['properties']
        for known_property in ESSENTIAL_PROPERTIES_KEYS:
            inspected_properties[known_property] = properties[known_property]
        node_properties = task.node.properties
        node_properties.update(inspected_properties)
        task.node.properties = node_properties

        # Inspect the hardware for additional hardware capabilities.
        # Since additional hardware capabilities may not apply to all the
        # hardwares, the method inspect_hardware() doesn't raise an error
        # for these capabilities.
        capabilities = _get_capabilities(task.node, ilo_object)
        if capabilities:
            valid_cap = _create_supported_capabilities_dict(capabilities)
            capabilities = _update_capabilities(task.node, valid_cap)
            if capabilities:
                node_properties['capabilities'] = capabilities
                task.node.properties = node_properties

        task.node.save()

        # Get the desired node inputs from the driver_info and create ports
        # as requested. It doesn't delete the ports because there is
        # no way for the operator to know which all MACs are associated
        # with the node and which are not. The proliantutils can
        # return only embedded NICs mac addresses and not the STANDUP NIC
        # cards. The port creation code is not excercised if
        # 'inspect_ports' == 'none'.

        driver_info = task.node.driver_info
        if (driver_info['inspect_ports']).lower() != 'none':
            macs_input_given = (
                _get_macs_for_desired_ports(task.node, result['macs']))

            if macs_input_given:
                # Create ports only for the requested ports.
                _create_ports_if_not_exist(task.node, macs_input_given)

        LOG.debug(("Node properties for %(node)s are updated as "
                   "%(properties)s"),
                   {'properties': inspected_properties,
                    'node': task.node.uuid})

        LOG.info(_LI("Node %s inspected."), task.node.uuid)
        if power_turned_on:
            conductor_utils.node_power_action(task, states.POWER_OFF)
            LOG.info(_LI("The node %s was powered on for inspection. "
                         "Powered off the node as inspection completed."),
                         task.node.uuid)
        return states.MANAGEABLE
