
from katcp.txprotocol import TxDeviceServer, ClientKatCP, TxDeviceProtocol
from twisted.internet.defer import Deferred, DeferredList
from twisted.internet import reactor
from twisted.internet.protocol import ClientFactory
from katcp import Message
from katcp.kattypes import request, return_reply, Int

import re, time

def value_only_formatted():
    """ A decorator that changes a value-only read into read_formatted
    format (using time.time and 'ok')
    """
    def decorator(func):
        def new_func(self):
            return time.time(), "ok", func(self)
        new_func.func_name = func.func_name
        return new_func
    return decorator

class ProxiedSensor(object):
    """ A sensor which is a proxy for other sensor on the remote device.
    Returns a deferred on read
    """
    def __init__(self, name, device, proxy):
        self.name = name
        self.device = device

    def read_formatted(self):
        return self.device.send_request('sensor-value', self.name)

class StateSensor(object):
    """ A device state sensor
    """
    def __init__(self, name, device):
        self.device = device
        self.name = name

    @value_only_formatted()
    def read_formatted(self):
        return DeviceHandler.STATE_NAMES[self.device.state]

class DeviceHandler(ClientKatCP):
    SYNCING, SYNCED, UNSYNCED = range(3)
    STATE_NAMES = ['syncing', 'synced', 'unsynced']

    TYPE = 'full'

    stopping = False
    
    def __init__(self, name, host, port):
        self.name = name
        self.host = host
        self.port = port
        ClientKatCP.__init__(self)
        self.requests = []
        self.sensors = []
        self.state = self.UNSYNCED
    
    def connectionMade(self):
        """ This is called after connection has been made. Introspect server
        about it's capabilities
        """
        def got_help((informs, reply)):
            for inform in informs:
                self.requests.append(inform.arguments[0])
            self.send_request('sensor-list').addCallback(got_sensor_list)

        def got_sensor_list((informs, reply)):
            self.state = self.SYNCED
            for inform in informs:
                self.sensors.append(inform.arguments[0])
                self.proxy.add_proxied_sensor(self, inform.arguments[0])
            self.proxy.device_ready(self)

        self.state = self.SYNCING
        self.send_request('help').addCallback(got_help)

    def add_proxy(self, proxy):
        self.proxy = proxy
        proxy.add_sensor(StateSensor(self.name + '-' + 'state', self))

    def schedule_resyncing(self):
        reactor.connectTCP(self.host, self.port, self.proxy.client_factory)

    def connectionLost(self, failure):
        self.state = self.UNSYNCED
        ClientKatCP.connectionLost(self, failure)
        if not self.stopping:
            reactor.callLater(1, self.schedule_resyncing)

class DeviceServer(TxDeviceProtocol):
    @request(include_msg=True)
    @return_reply(Int(min=0))
    def request_device_list(self, reqmsg):
        """Return a list of devices aggregated by the proxy.

        Returns the list of devices a sequence of #device-list informs.

        Inform Arguments
        ----------------
        device : str
            Name of a device.

        Returns
        -------
        success : {'ok', 'fail'}
            Whether sending the list of devices succeeded.
        informs : int
            Number of #device-list informs sent.

        Examples
        --------
        ?device-list
        #device-list antenna
        #device-list enviro
        !device-list ok 2
        """
        for name in sorted(self.factory.devices):
            self.send_message(Message.inform("device-list", name,
                              self.factory.devices[name].TYPE))
        return "ok", len(self.factory.devices)

    def request_sensor_list(self, msg):
        """Request the list of sensors.

        The list of sensors is sent as a sequence of #sensor-list informs.

        Parameters
        ----------
        name : str or pattern, optional
            If the name is not a pattern, list just the sensor with the given name.
            A pattern starts and ends with a slash ('/') and uses the Python re
            module's regular expression syntax. All sensors whose names contain the
            pattern are listed.  The default is to list all sensors.

        Inform Arguments
        ----------------
        name : str
            The name of the sensor being described.
        description : str
            Description of the named sensor.
        units : str
            Units for the value of the named sensor.
        type : str
            Type of the named sensor.
        params : list of str, optional
            Additional sensor parameters (type dependent). For integer and float
            sensors the additional parameters are the minimum and maximum sensor
            value. For discrete sensors the additional parameters are the allowed
            values. For all other types no additional parameters are sent.

        Returns
        -------
        success : {'ok', 'fail'}
            Whether sending the sensor list succeeded.
        informs : int
            Number of #sensor-list inform messages sent.

        Examples
        --------
        ?sensor-list
        #sensor-list psu.voltage PSU\_voltage. V float 0.0 5.0
        #sensor-list cpu.status CPU\_status. \@ discrete on off error
        ...
        !sensor-list ok 5

        ?sensor-list /voltage/
        #sensor-list psu.voltage PSU\_voltage. V float 0.0 5.0
        #sensor-list cpu.voltage CPU\_voltage. V float 0.0 3.0
        !sensor-list ok 2

        ?sensor-list cpu.power.on
        #sensor-list cpu.power.on Whether\_CPU\_hase\_power. \@ boolean
        !sensor-list ok 1
        """
        # handle non-regex cases
        raise NotImplementedError
        if not msg.arguments or not (msg.arguments[0].startswith("/")
            and msg.arguments[0].endswith("/")):
            return TxDeviceServer.request_sensor_list(self, msg)

        # handle regex
        name_re = re.compile(msg.arguments[0][1:-1])
        sensors = dict([(name, sensor) for name, sensor in
            self._sensors.iteritems() if name_re.search(name)])

        for name, sensor in sorted(sensors.items(), key=lambda x: x[0]):
            self.send_message(Message.inform("sensor-list",
                name, sensor.description, sensor.units, sensor.stype,
                *sensor.formatted_params))

        return Message.reply(msg.name, "ok", len(sensors))

    def _send_all_sensors(self, filter=None):
        """ Sends all sensor values with given filter (None = all)
        """
        counter = [0] # this has to be a list or an object, thanks to
        # python lexical scoping rules (we could not write count += 1
        # in a function)
        
        def device_ok((informs, reply)):
            for inform in informs:
                if filter is None or re.match(filter, inform.arguments[2]):
                    self.send_message(inform)
                    counter[0] += 1

        def all_ok(_):
            self.send_message(Message.reply('sensor-value', 'ok',
                                            str(counter[0])))

        wait_for = []
        for device in self.factory.devices.itervalues():
            if device.state == device.SYNCED:
                d = device.send_request('sensor-value')
                d.addCallback(device_ok)
                wait_for.append(d)
            # otherwise we don't have the list of sensors, so we don't
            # send the message
        DeferredList(wait_for).addCallback(all_ok)
        for sensor in self.factory.sensors.itervalues():
            if not isinstance(sensor, ProxiedSensor):
                if filter is None or re.match(filter, sensor.name):
                    timestamp_ms, status, value = sensor.read_formatted()
                    counter[0] += 1
                    self.send_message(Message.inform('sensor-value',
                                                     timestamp_ms, "1",
                                                     sensor.name, status,
                                                     value))


    def request_sensor_value(self, msg):
        """Poll a sensor value or value(s).

        A list of sensor values as a sequence of #sensor-value informs.

        Parameters
        ----------
        name : str or pattern, optional
            If the name is not a pattern, list just the values of sensors with the
            given name.  A pattern starts and ends with a slash ('/') and uses the
            Python re module's regular expression syntax. The values of all sensors
            whose names contain the pattern are listed.  The default is to list the
            values of all sensors.

        Inform Arguments
        ----------------
        timestamp : float
            Timestamp of the sensor reading in milliseconds since the Unix epoch.
        count : {1}
            Number of sensors described in this #sensor-value inform. Will always
            be one. It exists to keep this inform compatible with #sensor-status.
        name : str
            Name of the sensor whose value is being reported.
        value : object
            Value of the named sensor. Type depends on the type of the sensor.

        Returns
        -------
        success : {'ok', 'fail'}
            Whether sending the list of values succeeded.
        informs : int
            Number of #sensor-value inform messages sent.

        Examples
        --------
        ?sensor-value
        #sensor-value 1244631611415.231 1 psu.voltage 4.5
        #sensor-value 1244631611415.200 1 cpu.status off
        ...
        !sensor-value ok 5

        ?sensor-value /voltage/
        #sensor-value 1244631611415.231 1 psu.voltage 4.5
        #sensor-value 1244631611415.100 1 cpu.voltage 4.5
        !sensor-value ok 2

        ?sensor-value cpu.power.on
        #sensor-value 1244631611415.231 1 cpu.power.on 0
        !sensor-value ok 1
        """
        def send_single_ok((informs, reply)):
            self.send_message(informs[0])
            self.send_message(reply)
        
        if not msg.arguments:
            self._send_all_sensors()
            return
        name = msg.arguments[0]
        if len(name) >= 2 and name.startswith("/") and name.endswith("/"):
            # regex case
            self._send_all_sensors(name[1:-1])
        else:
            sensor = self.factory.sensors.get(name, None)
            if sensor is None:
                return Message.reply(msg.name, "fail", "Unknown sensor name.")
            res = sensor.read_formatted()
            if isinstance(res, Deferred):
                res.addCallback(send_single_ok)
                return
            timestamp_ms, status, value = res
            send_single_ok(([Message.inform('sensor-value', timestamp_ms, "1",
                                            name, status, value)],
                            Message.reply(msg.name, "ok", "1")))
            return

    def __getattr__(self, attr):
        def request_returned((informs, reply)):
            assert informs == [] # for now
            # we *could* in theory just change message name, but let's copy
            # just in case
            self.send_message(Message.reply(dev_name + "-" + req_name,
                                            *reply.arguments))

        def request_failed(failure):
            self.send_message(Message.reply(dev_name + '-' + req_name,
                                            "fail", "Device not synced"))

        def callback(msg):
            if device.state is device.UNSYNCED:
                return Message.reply(dev_name + "-" + req_name, "fail",
                                     "Device not synced")
            d = device.send_request(req_name, *msg.arguments)
            d.addCallbacks(request_returned, request_failed)
                
        if not attr.startswith('request_'):
            return object.__getattribute__(self, attr)
        lst = attr.split('_')
        if len(lst) < 3:
            return object.__getattribute__(self, attr)
        dev_name = lst[1]
        device = self.factory.devices.get(dev_name, None)
        if device is None:
            return object.__getattribute__(self, attr)
        req_name = "_".join(lst[2:])
        return callback

class ClientDeviceFactory(ClientFactory):
    """ A factory that does uses prebuilt device handler objects
    """
    def __init__(self, addr_mapping):
        self.addr_mapping = addr_mapping # shared mapping with ProxyKatCP

    def buildProtocol(self, addr):
        return self.addr_mapping[(addr.host, addr.port)]

class ProxyKatCP(TxDeviceServer):
    """ This is a proxy class that will listen on a given host and port
    providing info about underlaying clients if needed
    """
    protocol = DeviceServer
    
    def __init__(self, *args, **kwds):
        TxDeviceServer.__init__(self, *args, **kwds)
        self.addr_mapping = {}
        self.client_factory = ClientDeviceFactory(self.addr_mapping)
        self.ready_devices = 0
        self.devices = {}
        self.setup_devices()
        self.scan_called = False
    
    def device_ready(self, device):
        self.ready_devices += 1
        if self.ready_devices == len(self.devices) and not self.scan_called:
            self.scan_called = True # one shot only
            self.devices_scan_complete()

    def add_device(self, device):
        """ Add a single device to the list of devices that we have
        """
        def really_add_device(addr):
            device.host = addr
            reactor.connectTCP(device.host, device.port, self.client_factory)
            self.devices[device.name] = device
            self.addr_mapping[(device.host, device.port)] = device
            device.add_proxy(self)

        reactor.resolve(device.host).addCallback(really_add_device)
        self.sensors

    def add_proxied_sensor(self, device, sensor_name):
        self.sensors[sensor_name] = ProxiedSensor(sensor_name, device, self)

    def devices_scan_complete(self, _):
        """ A callback called when devices are properly set up and read.
        Override if needed
        """
        pass

    def setup_devices(self):
        raise NotImplementedError("Override this to provide devices setup")

    def stop(self):
        for device in self.devices.values():
            if device.state != device.UNSYNCED:
                device.stopping = True
                device.transport.loseConnection(None)
        self.port.stopListening()
