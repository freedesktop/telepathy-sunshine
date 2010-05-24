# telepathy-sunshine is the GaduGadu connection manager for Telepathy
#
# Copyright (C) 2006-2007 Ali Sabil <ali.sabil@gmail.com>
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

import logging
import imghdr
import hashlib
import dbus
import StringIO
from sunshine.lqsoft.gaduapi import *

import telepathy

from twisted.web.client import getPage

from xml.dom import minidom

from sunshine.handle import SunshineHandleFactory
from sunshine.util.decorator import async

__all__ = ['SunshineAvatars']

logger = logging.getLogger('Sunshine.Avatars')

SUPPORTED_AVATAR_MIME_TYPES = dbus.Array(["image/png", "image/jpeg", "image/gif"], signature='s')
MINIMUM_AVATAR_PIXELS = dbus.UInt32(96)
RECOMMENDED_AVATAR_PIXELS = dbus.UInt32(96)
MAXIMUM_AVATAR_PIXELS = dbus.UInt32(256)
MAXIMUM_AVATAR_BYTES = dbus.UInt32(500 * 1024)

class SunshineAvatars(telepathy.server.ConnectionInterfaceAvatars):

    def __init__(self):
        print 'SunshineAvatars called.'
        self._avatar_known = False
        telepathy.server.ConnectionInterfaceAvatars.__init__(self)
        
        dbus_interface = telepathy.CONNECTION_INTERFACE_AVATARS
        self._implement_property_get(dbus_interface, {
            'SupportedAvatarMIMETypes':
            lambda: SUPPORTED_AVATAR_MIME_TYPES,
            'MinimumAvatarHeight': lambda: MINIMUM_AVATAR_PIXELS,
            'MinimumAvatarWidth': lambda: MINIMUM_AVATAR_PIXELS,
            'RecommendedAvatarHeight': lambda: RECOMMENDED_AVATAR_PIXELS,
            'RecommendedAvatarWidth': lambda: RECOMMENDED_AVATAR_PIXELS,
            'MaximumAvatarHeight': lambda: MAXIMUM_AVATAR_PIXELS,
            'MaximumAvatarWidth': lambda: MAXIMUM_AVATAR_PIXELS,
            'MaximumAvatarBytes': lambda: MAXIMUM_AVATAR_BYTES,
        })

    def GetAvatarRequirements(self):
        return (SUPPORTED_AVATAR_MIME_TYPES,
                MINIMUM_AVATAR_PIXELS, MINIMUM_AVATAR_PIXELS,
                MAXIMUM_AVATAR_PIXELS, MAXIMUM_AVATAR_PIXELS,
                MAXIMUM_AVATAR_BYTES)

    def GetKnownAvatarTokens(self, contacts):
        result = {}
        for handle_id in contacts:
            handle = self.handle(telepathy.HANDLE_TYPE_CONTACT, handle_id)
            if handle == self.GetSelfHandle():
                #tutaj kiedys trzeba napisac kod odp za naszego avatara
                contact = None
                result[handle] = handle.name

            else:
                contact = handle.contact

                if contact is not None:
                    av_token = str(handle.name)
                else:
                    av_token = None
    
                if av_token is not None:
                    result[handle] = av_token
                elif self._avatar_known:
                    result[handle] = ""
        return result

    def RequestAvatars(self, contacts):
        for handle_id in contacts:
            handle = self.handle(telepathy.HANDLE_TYPE_CONTACT, handle_id)
            
            url = 'http://api.gadu-gadu.pl/avatars/%s/0.xml' % (str(handle.name))
            d = getPage(url, timeout=10)
            d.addCallback(self.on_fetch_avatars_file_ok, url, handle_id)
            d.addErrback(self.on_fetch_avatars_file_failed, url, handle_id)
                        
    def SetAvatar(self, avatar, mime_type):
        if check_requirements() == True:
            if not isinstance(avatar, str):
                avatar = "".join([chr(b) for b in avatar])
                data = StringIO.StringIO(avatar).getvalue()
                ext = self.ggapi.getExtByType(mime_type)
                self.ggapi.uploadAvatar(data, ext)
        return str(self.profile.uin).encode("hex")

    def ClearAvatar(self):
        pass
#        self.msn_client.profile.msn_object = None
#        self._avatar_known = True

    def getAvatar(self, sender, url):
        logger.info("getAvatar: %s %s" % (sender, url))
        handle_id = self.get_handle_id_by_name(telepathy.constants.HANDLE_TYPE_CONTACT, str(sender))
        
        if handle_id != 0:
            d = getPage(str(url), timeout=20)
            d.addCallback(self.on_fetch_avatars_ok, str(url), handle_id)
            d.addErrback(self.on_fetch_avatars_failed, str(url), handle_id)

    def on_fetch_avatars_file_ok(self, result, url, handle_id):
        try:
            if result:
                logger.info("Avatar file retrieved from %s" % (url))
                e = minidom.parseString(result)
                if e.getElementsByTagName('avatar')[0].attributes["blank"].value != '1':
                    data = e.getElementsByTagName('bigAvatar')[0].firstChild.data
    
                    d = getPage(str(data), timeout=20)
                    d.addCallback(self.on_fetch_avatars_ok, data, handle_id)
                    d.addErrback(self.on_fetch_avatars_failed, data, handle_id)
        except:
            logger.info("Avatar file can't be retrieved from %s" % (url))

    def on_fetch_avatars_file_failed(self, error, url, handle_id):
        logger.info("Avatar file can't be retrieved from %s, error: %s" % (url, error.getErrorMessage()))

    def on_fetch_avatars_ok(self, result, url, handle_id):
        try:
            handle = self.handle(telepathy.constants.HANDLE_TYPE_CONTACT, handle_id)
            logger.info("Avatar retrieved for %s from %s" % (handle.name, url))
            type = imghdr.what('', result)
            if type is None: type = 'jpeg'
            avatar = dbus.ByteArray(result)
            h = hashlib.new('md5')
            h.update(url)
            token = h.hexdigest()
            self.AvatarRetrieved(handle, token, avatar, 'image/' + type)
        except:
            logger.debug("Avatar retrieved but something went wrong.")

    def on_fetch_avatars_failed(self, error, url, handle_id):
        logger.debug("Avatar not retrieved, error: %s" % (error.getErrorMessage()))
