# telepathy-sunshine is the GaduGadu connection manager for Telepathy
#
# Copyright (C) 2010 Krzysztof Klinikowski <kkszysiu@gmail.com>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA

import sys
import os
import time
import weakref
import logging

import xml.etree.ElementTree as ET

from sunshine.util.config import SunshineConfig

from sunshine.lqsoft.pygadu.twisted_protocol import GaduClient
from sunshine.lqsoft.pygadu.models import GaduProfile, GaduContact, GaduContactGroup

from sunshine.lqsoft.gaduapi import *

from twisted.internet import reactor, protocol
from twisted.web.client import getPage
from twisted.internet import task
from twisted.python import log
from twisted.internet import threads

import dbus
import telepathy

from sunshine.presence import SunshinePresence
from sunshine.aliasing import SunshineAliasing
from sunshine.avatars import SunshineAvatars
from sunshine.handle import SunshineHandleFactory
from sunshine.capabilities import SunshineCapabilities
from sunshine.contacts_info import SunshineContactInfo
from sunshine.contacts import SunshineContacts
from sunshine.channel_manager import SunshineChannelManager
from sunshine.util.decorator import async, stripHTML, unescape

__all__ = ['GaduClientFactory', 'SunshineConnection']

logger = logging.getLogger('Sunshine.Connection')
observer = log.PythonLoggingObserver(loggerName='Sunshine.Connection')
observer.start()

#SSL
ssl_support = False

try:
    from OpenSSL import crypto, SSL
    from twisted.internet import ssl
    ssl_support = True
except ImportError:
    ssl_support = False
try:
    if ssl and ssl.supported:
        ssl_support = True
except NameError:
    ssl_support = False


if ssl_support == False:
    logger.info('SSL unavailable. Falling back to normal non-SSL connection.')
else:
    logger.info('Using SSL-like connection.')

class GaduClientFactory(protocol.ClientFactory):
    def __init__(self, config):
        self.config = config

    def buildProtocol(self, addr):
        # connect using current selected profile
        return GaduClient(self.config)

    def startedConnecting(self, connector):
        logger.info('Started to connect.')

    def clientConnectionLost(self, connector, reason):
        logger.info('Lost connection.  Reason: %s' % (reason))
        try:
	    if self.config.contactsLoop != None:
		self.config.contactsLoop.stop()
		self.config.contactsLoop = None
	    if self.config.exportLoop != None:
		self.config.exportLoop.stop()
		self.config.exportLoop = None
	except:
	    logger.info("Loops was not running")
        if reactor.running:
            reactor.stop()
            os._exit(1)

    def clientConnectionFailed(self, connector, reason):
        logger.info('Connection failed. Reason: %s' % (reason))
        try:
	    if self.config.contactsLoop != None:
		self.config.contactsLoop.stop()
		self.config.contactsLoop = None
	    if self.config.exportLoop != None:
		self.config.exportLoop.stop()
		self.config.exportLoop = None
	except:
	    logger.info("Loops was not running")
	if reactor.running:
            reactor.stop()
            os._exit(1)

class SunshineConnection(telepathy.server.Connection,
        telepathy.server.ConnectionInterfaceRequests,
        SunshinePresence,
        SunshineAliasing,
        SunshineAvatars,
        SunshineCapabilities,
        SunshineContactInfo,
        SunshineContacts
        ):

    def __init__(self, protocol, manager, parameters):
        protocol.check_parameters(parameters)

        try:
            account = unicode(parameters['account'])
            server = (parameters['server'], parameters['port'])

            self._manager = weakref.proxy(manager)
            self._account = (parameters['account'], parameters['password'])
            self.param_server = (parameters['server'], parameters['port'])
            self._export_contacts = bool(parameters['export-contacts'])
            self.param_use_ssl = bool(parameters['use-ssl'])
            self.param_specified_server = bool(parameters['use-specified-server'])

            self.profile = GaduProfile(uin= int(parameters['account']) )
            self.profile.uin = int(parameters['account'])
            self.profile.password = str(parameters['password'])
            self.profile.status = 0x014
            self.profile.onLoginSuccess = self.on_loginSuccess
            self.profile.onLoginFailure = self.on_loginFailed
            self.profile.onContactStatusChange = self.on_updateContact
            self.profile.onMessageReceived = self.on_messageReceived
            self.profile.onTypingNotification = self.onTypingNotification
            self.profile.onXmlAction = self.onXmlAction
            self.profile.onXmlEvent = self.onXmlEvent
            self.profile.onUserData = self.onUserData

            #lets try to make file with contacts etc ^^
            self.configfile = SunshineConfig(int(parameters['account']))
            self.configfile.check_dirs()
            #lets get contacts from contacts config file
            contacts_list = self.configfile.get_contacts()

            for contact_from_list in contacts_list['contacts']:
                c = GaduContact.from_xml(contact_from_list)
                try:
                    c.uin
                    self.profile.addContact(c)
                except:
                    pass

            for group_from_list in contacts_list['groups']:
                g = GaduContactGroup.from_xml(group_from_list)
                if g.Name:
                    self.profile.addGroup(g)
            
            logger.info("We have %s contacts in file." % (self.configfile.get_contacts_count()))
            
            self.factory = GaduClientFactory(self.profile)
            if check_requirements() == True:
                self.ggapi = GG_Oauth(self.profile.uin, parameters['password'])
            
            self._channel_manager = SunshineChannelManager(self, protocol)

            self._recv_id = 0
            self._conf_id = 0
            self.pending_contacts_to_group = {}
            self._status = None
            self.profile.contactsLoop = None
            
            # Call parent initializers
            telepathy.server.Connection.__init__(self, 'gadugadu', account, 'sunshine', protocol)
            telepathy.server.ConnectionInterfaceRequests.__init__(self)
            SunshinePresence.__init__(self)
            SunshineAvatars.__init__(self)
            SunshineCapabilities.__init__(self)
            if check_requirements() == True:
                SunshineContactInfo.__init__(self)
            SunshineContacts.__init__(self)
            
            self.updateCapabilitiesCalls()

            self.set_self_handle(SunshineHandleFactory(self, 'self'))

            self.__disconnect_reason = telepathy.CONNECTION_STATUS_REASON_NONE_SPECIFIED
            #small hack. We started to connnect with status invisible and just later we change status to client-like
            self._initial_presence = 0x014
            self._initial_personal_message = None
            self._personal_message = ''

            self.conn_checker = task.LoopingCall(self.connection_checker)
            self.conn_checker.start(5.0, False)

            logger.info("Connection to the account %s created" % account)
        except Exception, e:
            import traceback
            logger.exception("Failed to create Connection")
            raise

    def connection_checker(self):
        if len(self.manager._connections) == 0:
            logger.info("Connection checker killed CM")
            #self.quit()
            reactor.stop()

    @property
    def manager(self):
        return self._manager

    @property
    def gadu_client(self):
        return self.profile

    def handle(self, handle_type, handle_id):
        self.check_handle(handle_type, handle_id)
        return self._handles[handle_type, handle_id]

    def get_contact_alias(self, handle_id):
        return self._get_alias(handle_id)

    def get_handle_id_by_name(self, handle_type, name):
        """Returns a handle ID for the given type and name

        Arguments:
        handle_type -- Telepathy Handle_Type for all the handles
        name -- username for the contact

        Returns:
        handle_id -- ID for the given username
        """
        handle_id = 0
        for handle in self._handles.values():
            if handle.get_name() == name and handle.type == handle_type:
                handle_id = handle.get_id()
                break

        return handle_id

    def Connect(self):
        if self._status == telepathy.CONNECTION_STATUS_DISCONNECTED:
            logger.info("Connecting")
            self.StatusChanged(telepathy.CONNECTION_STATUS_CONNECTING,
                    telepathy.CONNECTION_STATUS_REASON_REQUESTED)
            self.__disconnect_reason = telepathy.CONNECTION_STATUS_REASON_NONE_SPECIFIED
            if self.param_specified_server:
                self.makeConnection(self.param_server[0], self.param_server[1])
            else:
                self.getServerAdress(self._account[0])

    def Disconnect(self):
        if self.profile.contactsLoop:
            self.profile.contactsLoop.stop()
            self.profile.contactsLoop = None
        if self._export_contacts == True:
            if self.profile.exportLoop:
                self.profile.exportLoop.stop()
                self.profile.exportLoop = None
        
        #if self._status == telepathy.CONNECTION_STATUS_DISCONNECTED:
        #    self.profile.disconnect()
        #    self.factory.disconnect()
        
        self.StatusChanged(telepathy.CONNECTION_STATUS_DISCONNECTED,
                telepathy.CONNECTION_STATUS_REASON_REQUESTED)
        self.profile.disconnect()

        logger.info("Disconnecting")

    def GetInterfaces(self):
        return self._interfaces

    def RequestHandles(self, handle_type, names, sender):
        logger.info("Method RequestHandles called, handle type: %s, names: %s" % (str(handle_type), str(names)))
        self.check_connected()
        self.check_handle_type(handle_type)
        
        handles = []
        for name in names:
            if handle_type == telepathy.HANDLE_TYPE_CONTACT:
                contact_name = name
                    
                try:
                    int(str(contact_name))
                except:
                    raise InvalidHandle
                
                handle_id = self.get_handle_id_by_name(telepathy.constants.HANDLE_TYPE_CONTACT, str(contact_name))

                if handle_id != 0:
                    handle = self.handle(telepathy.constants.HANDLE_TYPE_CONTACT, handle_id)
                else:
                    handle = SunshineHandleFactory(self, 'contact',
                            str(contact_name), None)
            elif handle_type == telepathy.HANDLE_TYPE_ROOM:
                handle = SunshineHandleFactory(self, 'room', name)
            elif handle_type == telepathy.HANDLE_TYPE_LIST:
                handle = SunshineHandleFactory(self, 'list', name)
            elif handle_type == telepathy.HANDLE_TYPE_GROUP:
                handle = SunshineHandleFactory(self, 'group', name)
            else:
                raise telepathy.NotAvailable('Handle type unsupported %d' % handle_type)
            handles.append(handle.id)
            self.add_client_handle(handle, sender)
        return handles

    def _generate_props(self, channel_type, handle, suppress_handler, initiator_handle=None):
        props = {
            telepathy.CHANNEL_INTERFACE + '.ChannelType': channel_type,
            telepathy.CHANNEL_INTERFACE + '.TargetHandle': 0 if handle is None else handle.get_id(),
            telepathy.CHANNEL_INTERFACE + '.TargetHandleType': telepathy.HANDLE_TYPE_NONE if handle is None else handle.get_type(),
            telepathy.CHANNEL_INTERFACE + '.Requested': suppress_handler
            }

        if initiator_handle is not None:
            props[telepathy.CHANNEL_INTERFACE + '.InitiatorHandle'] = initiator_handle.id

        return props

    @dbus.service.method(telepathy.CONNECTION, in_signature='suub',
        out_signature='o', async_callbacks=('_success', '_error'))
    def RequestChannel(self, type, handle_type, handle_id, suppress_handler,
            _success, _error):
        self.check_connected()
        channel_manager = self._channel_manager

        if handle_id == 0:
            handle = None
        else:
            handle = self.handle(handle_type, handle_id)
        props = self._generate_props(type, handle, suppress_handler)
        self._validate_handle(props)

        channel = channel_manager.channel_for_props(props, signal=False)

        _success(channel._object_path)
        self.signal_new_channels([channel])

    #@async
    #@deferred
    def updateContactsFile(self):
        """Method that updates contact file when it changes and in loop every 5 seconds."""
        reactor.callInThread(self.configfile.make_contacts_file, self.profile.groups, self.profile.contacts)
        #self.configfile.make_contacts_file(self.profile.groups, self.profile.contacts)

    #@async
    #@deferred
    def exportContactsFile(self):
        logger.info("Exporting contacts.")
        # TODO: make fully non-blocking
        file = open(self.configfile.path, "r")
        contacts_xml = file.read()
        file.close()
        if len(contacts_xml) != 0:
	    reactor.callInThread(self.profile.exportContacts, contacts_xml)
            #self.profile.exportContacts(contacts_xml)

    @async
    def makeTelepathyContactsChannel(self):
        logger.debug("Method makeTelepathyContactsChannel called.")
        handle = SunshineHandleFactory(self, 'list', 'subscribe')
        props = self._generate_props(telepathy.CHANNEL_TYPE_CONTACT_LIST,
            handle, False)
        self._channel_manager.channel_for_props(props, signal=True)

    @async
    def makeTelepathyGroupChannels(self):
        logger.debug("Method makeTelepathyGroupChannels called.")
        for group in self.profile.groups:
            handle = SunshineHandleFactory(self, 'group',
                    group.Name)
            props = self._generate_props(
                telepathy.CHANNEL_TYPE_CONTACT_LIST, handle, False)
            self._channel_manager.channel_for_props(props, signal=True)

    def getServerAdress(self, uin):
        logger.info("Fetching GG server adress.")
        url = 'http://appmsg.gadu-gadu.pl/appsvc/appmsg_ver10.asp?fmnumber=%s&lastmsg=0&version=10.1.1.11119' % (str(uin))
        d = getPage(url, timeout=10)
        d.addCallback(self.on_server_adress_fetched, uin)
        d.addErrback(self.on_server_adress_fetched_failed, uin)

    def makeConnection(self, ip, port):
        logger.info("%s %s %s" % (ip, port, self.param_use_ssl))
        if ssl_support and self.param_use_ssl:
            self.ssl = ssl.CertificateOptions(method=SSL.SSLv3_METHOD)
            reactor.connectSSL(ip, port, self.factory, self.ssl)
        else:
            reactor.connectTCP(ip, port, self.factory)

    def on_server_adress_fetched(self, result, uin):
        try:
            result = result.replace('\n', '')
            a = result.split(' ')
            if a[0] == '0' and a[-1:][0] != 'notoperating':
                addr = a[-1:][0]
                logger.info("GG server adress fetched, IP: %s" % (addr))
                if ssl_support and self.param_use_ssl:
                    port = 443
                    self.makeConnection(addr, port)
                else:
                    port = 8074
                    self.makeConnection(addr, port)
            else:
                raise Exception()
        except:
            logger.debug("Cannot get GG server IP adress. Trying again...")
            self.getServerAdress(uin)

    def on_server_adress_fetched_failed(self, error, uin):
        logger.error("Failed to get page with server IP adress.")
        self.StatusChanged(telepathy.CONNECTION_STATUS_DISCONNECTED,
                telepathy.CONNECTION_STATUS_REASON_NETWORK_ERROR)
        self._manager.disconnected(self)
        #self.factory.disconnect()

    def on_contactsImported(self):
        logger.info("No contacts in the XML contacts file yet. Contacts imported.")

        #self.configfile.make_contacts_file(self.profile.groups, self.profile.contacts)
        self.profile.contactsLoop = task.LoopingCall(self.updateContactsFile)
        self.profile.contactsLoop.start(5.0, True)

        if self._export_contacts == True:
            self.profile.exportLoop = task.LoopingCall(self.exportContactsFile)
            self.profile.exportLoop.start(30.0)

        self.makeTelepathyContactsChannel()
        self.makeTelepathyGroupChannels()
        
        SunshineAliasing.__init__(self)
            
        self._status = telepathy.CONNECTION_STATUS_CONNECTED
        self.StatusChanged(telepathy.CONNECTION_STATUS_CONNECTED,
                telepathy.CONNECTION_STATUS_REASON_REQUESTED)

    def on_loginSuccess(self):
        logger.info("Connected")

        #if its a first run or we dont have any contacts in contacts file yet then try to import contacts from server
        if self.configfile.get_contacts_count() == 0:
            self.profile.importContacts(self.on_contactsImported)
        else:
            #self.configfile.make_contacts_file(self.profile.groups, self.profile.contacts)
            self.profile.contactsLoop = task.LoopingCall(self.updateContactsFile)
            self.profile.contactsLoop.start(5.0, True)
            
            if self._export_contacts == True:
                self.profile.exportLoop = task.LoopingCall(self.exportContactsFile)
                self.profile.exportLoop.start(30.0)

            self.makeTelepathyContactsChannel()
            self.makeTelepathyGroupChannels()

            SunshineAliasing.__init__(self)
    
            self._status = telepathy.CONNECTION_STATUS_CONNECTED
            self.StatusChanged(telepathy.CONNECTION_STATUS_CONNECTED,
                    telepathy.CONNECTION_STATUS_REASON_REQUESTED)
        #self._populate_capabilities()
        #self.contactAdded(self.GetSelfHandle())

    def on_loginFailed(self, response):
        logger.info("Login failed: ", response)
        self._status = telepathy.CONNECTION_STATUS_DISCONNECTED
        self.StatusChanged(telepathy.CONNECTION_STATUS_DISCONNECTED,
                telepathy.CONNECTION_STATUS_REASON_AUTHENTICATION_FAILED)
        reactor.stop()

    #@async
    def on_updateContact(self, contact):
        #handle = SunshineHandleFactory(self, 'contact', str(contact.uin))
        handle_id = self.get_handle_id_by_name(telepathy.constants.HANDLE_TYPE_CONTACT, str(contact.uin))
        handle = self.handle(telepathy.constants.HANDLE_TYPE_CONTACT, handle_id)
        logger.info("Method on_updateContact called, status changed for UIN: %s, id: %s, status: %s, description: %s" % (contact.uin, handle.id, contact.status, contact.get_desc()))
        self._presence_changed(handle, contact.status, contact.get_desc())

    #@async
    def on_messageReceived(self, msg):
        if hasattr(msg.content.attrs, 'conference') and msg.content.attrs.conference != None:
            recipients = msg.content.attrs.conference.recipients
            recipients = map(str, recipients)
            recipients.append(str(msg.sender))
            recipients = sorted(recipients)
            conf_name = ', '.join(map(str, recipients))

            #active handle for current writting contact
            ahandle_id = self.get_handle_id_by_name(telepathy.constants.HANDLE_TYPE_CONTACT,
                                              str(msg.sender))

            if ahandle_id != 0:
                ahandle = self.handle(telepathy.constants.HANDLE_TYPE_CONTACT, ahandle_id)
            else:
                ahandle = SunshineHandleFactory(self, 'contact',
                        str(msg.sender), None)

            #now we need to preapare a new room and make initial users in it
            room_handle_id = self.get_handle_id_by_name(telepathy.constants.HANDLE_TYPE_ROOM, str(conf_name))

            handles = []
            
            if room_handle_id == 0:
                room_handle =  SunshineHandleFactory(self, 'room', str(conf_name))

                for number in recipients:
                    handle_id = self.get_handle_id_by_name(telepathy.constants.HANDLE_TYPE_CONTACT,
                                              number)
                    if handle_id != 0:
                        handle = self.handle(telepathy.constants.HANDLE_TYPE_CONTACT, handle_id)
                    else:
                        handle = SunshineHandleFactory(self, 'contact',
                                number, None)

                    handles.append(handle)
            else:
                room_handle = self.handle(telepathy.constants.HANDLE_TYPE_ROOM, room_handle_id)

            props = self._generate_props(telepathy.CHANNEL_TYPE_TEXT,
                    room_handle, False)

            if handles:
                channel = self._channel_manager.channel_for_props(props,
                        signal=True, conversation=handles)
                channel.MembersChanged('', handles, [], [], [],
                        0, telepathy.CHANNEL_GROUP_CHANGE_REASON_NONE)
            else:
                channel = self._channel_manager.channel_for_props(props,
                        signal=True, conversation=None)

            if int(msg.content.klass) == 9:
                timestamp = int(msg.time)
            else:
                timestamp = int(time.time())
            type = telepathy.CHANNEL_TEXT_MESSAGE_TYPE_NORMAL
            logger.info("User %s sent a message" % ahandle.name)

            logger.info("Msg from %r %d %d [%r] [%r]" % (msg.sender, msg.content.offset_plain, msg.content.offset_attrs, msg.content.plain_message, msg.content.html_message))

            if msg.content.html_message:
                #we need to strip all html tags
                text = unescape(stripHTML(msg.content.html_message))
            else:
                text = unescape((msg.content.plain_message).decode('windows-1250'))


            message = "%s" % unicode(str(text).replace('\x00', '').replace('\r', ''))
            #print 'message: ', message
            channel.Received(self._recv_id, timestamp, ahandle, type, 0, message)
            self._recv_id += 1

        else:
            handle_id = self.get_handle_id_by_name(telepathy.constants.HANDLE_TYPE_CONTACT,
                                      str(msg.sender))
            if handle_id != 0:
                handle = self.handle(telepathy.constants.HANDLE_TYPE_CONTACT, handle_id)
            else:
                handle = SunshineHandleFactory(self, 'contact',
                        str(msg.sender), None)

            if int(msg.content.klass) == 9:
                timestamp = int(msg.time)
            else:
                timestamp = int(time.time())
            type = telepathy.CHANNEL_TEXT_MESSAGE_TYPE_NORMAL
            logger.info("User %s sent a message" % handle.name)

            logger.info("Msg from %r %d %d [%r] [%r]" % (msg.sender, msg.content.offset_plain, msg.content.offset_attrs, msg.content.plain_message, msg.content.html_message))

            props = self._generate_props(telepathy.CHANNEL_TYPE_TEXT,
                    handle, False)
            channel = self._channel_manager.channel_for_props(props,
                    signal=True, conversation=None)

            if msg.content.html_message:
                #we need to strip all html tags
                text = unescape(stripHTML(msg.content.html_message.replace('<br>', '\n')))
            else:
                text = unescape((msg.content.plain_message).decode('windows-1250'))


            message = "%s" % unicode(str(text).replace('\x00', '').replace('\r', ''))

            channel.signalTextReceived(self._recv_id, timestamp, handle, type, 0, handle.name, message)
            self._recv_id += 1
            
    def onTypingNotification(self, data):
        logger.info("TypingNotification uin=%d, type=%d" % (data.uin, data.type))
        
        handle_id = self.get_handle_id_by_name(telepathy.constants.HANDLE_TYPE_CONTACT,
                                  str(data.uin))
        if handle_id != 0:
            handle = self.handle(telepathy.constants.HANDLE_TYPE_CONTACT, handle_id)

            props = self._generate_props(telepathy.CHANNEL_TYPE_TEXT,
                    handle, False)
            channel = self._channel_manager.channel_for_props(props,
                    signal=True, conversation=None)
            
            if type == 0:
                channel.ChatStateChanged(handle, telepathy.CHANNEL_CHAT_STATE_PAUSED)
            elif type >= 1:
                channel.ChatStateChanged(handle, telepathy.CHANNEL_CHAT_STATE_COMPOSING)
                reactor.callLater(3, channel.ChatStateChanged, handle, telepathy.CHANNEL_CHAT_STATE_PAUSED)

    def onXmlAction(self, xml):
        logger.info("XmlAction: %s" % xml.data)

        #event occurs when user from our list change avatar
        #<events>
        #    <event id="12989655759719404037">
        #        <type>28</type>
        #        <sender>4634020</sender>
        #        <time>1270577383</time>
        #        <body></body>
        #        <bodyXML>
        #            <smallAvatar>http://avatars.gadu-gadu.pl/small/4634020?ts=1270577383</smallAvatar>
        #        </bodyXML>
        #    </event>
        #</events>
        try:
            tree = ET.fromstring(xml.data)
            core = tree.find("event")
            type = core.find("type").text
            if type == '28':
                sender = core.find("sender").text
                url = core.find("bodyXML/smallAvatar").text
                logger.info("XMLAction: Avatar Update")
                self.getAvatar(sender, url)
        except:
            pass

    def onXmlEvent(self, xml):
        logger.info("XmlEvent: %s" % xml,data)

    def onUserData(self, data):
        logger.info("UserData: %s" % str(data))
        #for user in data.users:
        #    print user.uin
        #    for attr in user.attr:
        #        print "%s - %s" % (attr.name, attr.value)
