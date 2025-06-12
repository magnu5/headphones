# This file is modified to work with headphones by CurlyMo <curlymoo1@gmail.com> as a part of XBian - XBMC on the Raspberry Pi

# Author: Nic Wolfe <nic@wolfeden.ca>
# URL: http://code.google.com/p/sickbeard/
#
# This file is part of Sick Beard.
#
# Sick Beard is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Sick Beard is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Sick Beard.  If not, see <http://www.gnu.org/licenses/>.


from base64 import standard_b64encode
import http.client
import xmlrpc.client

import headphones
from headphones import logger


def checkCompleted(nzb_id):
    """
    Check if an NZB has completed downloading with robust error handling.
    
    Args:
        nzb_id: ID or name of the NZB to check
        
    Returns:
        dict: {'completed': bool, 'progress': float, 'status': str} or None if error
    """
    if not nzb_id:
        logger.error("NZBget checkCompleted called with empty nzb_id")
        return None
        
    if not headphones.CONFIG.NZBGET_HOST:
        logger.error("No NZBget host found in configuration.")
        return None
        
    nzbgetXMLrpc = "%(protocol)s://%(username)s:%(password)s@%(host)s/xmlrpc"
    
    try:
        if headphones.CONFIG.NZBGET_HOST.startswith('https://'):
            protocol = 'https'
            host = headphones.CONFIG.NZBGET_HOST.replace('https://', '', 1)
        else:
            protocol = 'http'
            host = headphones.CONFIG.NZBGET_HOST.replace('http://', '', 1)

        url = nzbgetXMLrpc % {"protocol": protocol, "host": host,
                              "username": headphones.CONFIG.NZBGET_USERNAME,
                              "password": headphones.CONFIG.NZBGET_PASSWORD}

        nzbGetRPC = xmlrpc.client.ServerProxy(url)
        
        # First check the download queue
        try:
            queue = nzbGetRPC.listgroups()
        except xmlrpc.client.Fault as e:
            logger.error(f"NZBget XML-RPC fault while checking queue: {e}")
            return None
        except Exception as e:
            logger.error(f"Error connecting to NZBget for queue check: {e}")
            return None
            
        if queue:
            for item in queue:
                try:
                    if str(item.get('NZBID')) == str(nzb_id) or item.get('NZBName') == str(nzb_id):
                        name = item.get('NZBName', 'unknown')
                        total_mb = item.get('FileSizeMB', 0)
                        remaining_mb = item.get('RemainingSizeMB', 0)
                        
                        # Calculate progress safely
                        try:
                            if total_mb > 0:
                                progress = max(0, (total_mb - remaining_mb) / total_mb)
                            else:
                                progress = 0
                        except (TypeError, ValueError, ZeroDivisionError):
                            progress = 0
                        
                        status = item.get('Status', 'unknown')
                        
                        # Still in queue, not completed
                        logger.debug(f"NZBget NZB {name}: {progress*100:.1f}% complete, status: {status} (in queue)")
                        
                        return {
                            'completed': False,
                            'progress': progress,
                            'status': status,
                            'name': name
                        }
                except Exception as e:
                    logger.warning(f"Error processing NZBget queue item: {e}")
                    continue
        
        # Check the history for completed downloads
        try:
            history = nzbGetRPC.history()
        except xmlrpc.client.Fault as e:
            logger.error(f"NZBget XML-RPC fault while checking history: {e}")
            return None
        except Exception as e:
            logger.error(f"Error connecting to NZBget for history check: {e}")
            return None
            
        if history:
            for item in history:
                try:
                    if str(item.get('NZBID')) == str(nzb_id) or item.get('Name') == str(nzb_id):
                        name = item.get('Name', 'unknown')
                        status = item.get('Status', 'unknown')
                        
                        # Status can be: SUCCESS, FAILURE, WARNING, etc.
                        completed = status == 'SUCCESS'
                        progress = 1.0 if completed else 0.0
                        
                        logger.debug(f"NZBget NZB {name}: status: {status} (in history)")
                        
                        return {
                            'completed': completed,
                            'progress': progress,
                            'status': status,
                            'name': name
                        }
                except Exception as e:
                    logger.warning(f"Error processing NZBget history item: {e}")
                    continue
        
        logger.warning(f"NZBget NZB with ID/name {nzb_id} not found in queue or history")
        return None
        
    except xmlrpc.client.ProtocolError as e:
        if "Unauthorized" in str(e):
            logger.error("NZBget authentication failed - check username/password")
        else:
            logger.error(f"NZBget protocol error: {e}")
        return None
    except Exception as e:
        logger.error(f"Error checking NZBget completion for {nzb_id}: {e}")
        return None


def sendNZB(nzb):
    addToTop = False
    nzbgetXMLrpc = "%(protocol)s://%(username)s:%(password)s@%(host)s/xmlrpc"

    if not headphones.CONFIG.NZBGET_HOST:
        logger.error("No NZBget host found in configuration. Please configure it.")
        return False

    if headphones.CONFIG.NZBGET_HOST.startswith('https://'):
        protocol = 'https'
        host = headphones.CONFIG.NZBGET_HOST.replace('https://', '', 1)
    else:
        protocol = 'http'
        host = headphones.CONFIG.NZBGET_HOST.replace('http://', '', 1)

    url = nzbgetXMLrpc % {"protocol": protocol, "host": host,
                          "username": headphones.CONFIG.NZBGET_USERNAME,
                          "password": headphones.CONFIG.NZBGET_PASSWORD}

    nzbGetRPC = xmlrpc.client.ServerProxy(url)
    try:
        if nzbGetRPC.writelog("INFO", "headphones connected to drop of %s any moment now." % (
                nzb.name + ".nzb")):
            logger.debug("Successfully connected to NZBget")
        else:
            logger.info("Successfully connected to NZBget, but unable to send a message" % (
                nzb.name + ".nzb"))

    except http.client.socket.error:
        logger.error(
            "Please check your NZBget host and port (if it is running). NZBget is not responding to this combination")
        return False

    except xmlrpc.client.ProtocolError as e:
        if e.errmsg == "Unauthorized":
            logger.error("NZBget password is incorrect.")
        else:
            logger.error("Protocol Error: " + e.errmsg)
        return False

    nzbcontent64 = None
    if nzb.resultType == "nzbdata":
        data = nzb.extraInfo[0]
        # NZBGet needs a string, not bytes
        nzbcontent64 = standard_b64encode(data).decode("utf-8")

    logger.info("Sending NZB to NZBget")
    logger.debug("URL: " + url)

    dupekey = ""
    dupescore = 0

    try:
        # Find out if nzbget supports priority (Version 9.0+), old versions beginning with a 0.x will use the old command
        nzbget_version_str = nzbGetRPC.version()
        nzbget_version = int(nzbget_version_str[:nzbget_version_str.find(".")])
        if nzbget_version == 0:
            if nzbcontent64 is not None:
                nzbget_result = nzbGetRPC.append(nzb.name + ".nzb",
                                                 headphones.CONFIG.NZBGET_CATEGORY, addToTop,
                                                 nzbcontent64)
            else:
                # from headphones.common.providers.generic import GenericProvider
                # if nzb.resultType == "nzb":
                #     genProvider = GenericProvider("")
                #     data = genProvider.getURL(nzb.url)
                #     if (data is None):
                #         return False
                #     nzbcontent64 = standard_b64encode(data)
                # nzbget_result = nzbGetRPC.append(nzb.name + ".nzb", headphones.CONFIG.NZBGET_CATEGORY, addToTop, nzbcontent64)
                return False
        elif nzbget_version == 12:
            if nzbcontent64 is not None:
                nzbget_result = nzbGetRPC.append(nzb.name + ".nzb",
                                                 headphones.CONFIG.NZBGET_CATEGORY,
                                                 headphones.CONFIG.NZBGET_PRIORITY, False,
                                                 nzbcontent64, False, dupekey, dupescore, "score")
            else:
                nzbget_result = nzbGetRPC.appendurl(nzb.name + ".nzb",
                                                    headphones.CONFIG.NZBGET_CATEGORY,
                                                    headphones.CONFIG.NZBGET_PRIORITY, False,
                                                    nzb.url, False, dupekey, dupescore, "score")
        # v13+ has a new combined append method that accepts both (url and content)
        # also the return value has changed from boolean to integer
        # (Positive number representing NZBID of the queue item. 0 and negative numbers represent error codes.)
        elif nzbget_version >= 13:
            nzbget_result = True if nzbGetRPC.append(nzb.name + ".nzb",
                                                     nzbcontent64 if nzbcontent64 is not None else nzb.url,
                                                     headphones.CONFIG.NZBGET_CATEGORY,
                                                     headphones.CONFIG.NZBGET_PRIORITY, False,
                                                     False, dupekey, dupescore,
                                                     "score") > 0 else False
        else:
            if nzbcontent64 is not None:
                nzbget_result = nzbGetRPC.append(nzb.name + ".nzb",
                                                 headphones.CONFIG.NZBGET_CATEGORY,
                                                 headphones.CONFIG.NZBGET_PRIORITY, False,
                                                 nzbcontent64)
            else:
                nzbget_result = nzbGetRPC.appendurl(nzb.name + ".nzb",
                                                    headphones.CONFIG.NZBGET_CATEGORY,
                                                    headphones.CONFIG.NZBGET_PRIORITY, False,
                                                    nzb.url)

        if nzbget_result:
            logger.debug("NZB sent to NZBget successfully")
            return True
        else:
            logger.error("NZBget could not add %s to the queue" % (nzb.name + ".nzb"))
            return False
    except:
        logger.error(
            "Connect Error to NZBget: could not add %s to the queue" % (nzb.name + ".nzb"))
        return False
