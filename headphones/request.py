#  This file is part of Headphones.
#
#  Headphones is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  Headphones is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with Headphones.  If not, see <http://www.gnu.org/licenses/>.

from xml.dom import minidom
import collections

import sys
from bs4 import BeautifulSoup
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from headphones import logger
import feedparser
import headphones
import headphones.lock
from bs4.builder import XMLParsedAsHTMLWarning
import warnings
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

# Configure session with connection pooling and retry strategy
session = requests.Session()
retry_strategy = Retry(
    total=3,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["HEAD", "GET", "OPTIONS"],
    backoff_factor=1
)
adapter = HTTPAdapter(
    max_retries=retry_strategy,
    pool_connections=20,
    pool_maxsize=20
)
session.mount("http://", adapter)
session.mount("https://", adapter)


# Disable SSL certificate warnings. We have our own handling
requests.packages.urllib3.disable_warnings()

# Dictionary with last request times, for rate limiting.
last_requests = collections.defaultdict(int)
fake_lock = headphones.lock.FakeLock()


def request_response(url, method="get", auto_raise=True,
                     whitelist_status_code=None, lock=fake_lock, **kwargs):
    """
    Convenient wrapper for `requests.get', which will capture the exceptions
    and log them. On success, the Response object is returned. In case of a
    exception, None is returned.

    Additionally, there is support for rate limiting. To use this feature,
    supply a tuple of (lock, request_limit). The lock is used to make sure no
    other request with the same lock is executed. The request limit is the
    minimal time between two requests (and so 1/request_limit is the number of
    requests per seconds).
    """

    # Convert whitelist_status_code to a list if needed
    if whitelist_status_code and type(whitelist_status_code) != list:
        whitelist_status_code = [whitelist_status_code]

    # Disable verification of SSL certificates if requested. Note: this could
    # pose a security issue!
    kwargs["verify"] = bool(headphones.CONFIG.VERIFY_SSL_CERT)

    # This fix is put in place for systems with broken SSL (like QNAP)
    if not headphones.CONFIG.VERIFY_SSL_CERT and sys.version_info >= (2, 7, 9):
        try:
            import ssl
            ssl._create_default_https_context = ssl._create_unverified_context
        except:
            pass

    # Map method to the session.XXX method for connection pooling
    request_method = getattr(session, method.lower())

    try:
        # Request URL and wait for response
        with lock:
            logger.debug(
                "Requesting URL via %s method: %s", method.upper(), url)
            response = request_method(url, **kwargs)

        # If status code != OK, then raise exception, except if the status code
        # is white listed.
        if whitelist_status_code and auto_raise:
            if response.status_code not in whitelist_status_code:
                try:
                    response.raise_for_status()
                except:
                    logger.debug(
                        "Response status code %d is not white "
                        "listed, raised exception", response.status_code)
                    raise
        elif auto_raise:
            response.raise_for_status()

        return response
    except requests.exceptions.SSLError as e:
        if kwargs["verify"]:
            logger.error(
                "Unable to connect to remote host because of a SSL error. "
                "It is likely that your system cannot verify the validity "
                "of the certificate. The remote certificate is either "
                "self-signed, or the remote server uses SNI. See the wiki for "
                "more information on this topic.")
        else:
            logger.error(
                "SSL error raised during connection, with certificate "
                "verification turned off: %s", e)
    except requests.ConnectionError:
        logger.error(
            "Unable to connect to remote host. Check if the remote "
            "host is up and running.")
    except requests.Timeout:
        logger.error(
            "Request timed out. The remote host did not respond in a timely "
            "manner.")
    except requests.HTTPError as e:
        if e.response is not None:
            if e.response.status_code >= 500:
                cause = "remote server error"
            elif e.response.status_code >= 400:
                cause = "local client error"
            else:
                # I don't think we will end up here, but for completeness
                cause = "unknown"

            logger.error(
                "Request raise HTTP error with status code %d (%s).",
                e.response.status_code, cause)

            # Debug response
            if headphones.VERBOSE:
                server_message(e.response)
        else:
            logger.error("Request raised HTTP error.")
    except requests.RequestException as e:
        logger.error("Request raised exception: %s", e)


def request_soup(url, **kwargs):
    """
    Wrapper for `request_response', which will return a BeatifulSoup object if
    no exceptions are raised.
    """

    parser = kwargs.pop("parser", "html.parser")
    response = request_response(url, **kwargs)

    if response is not None:
        return BeautifulSoup(response.content, parser)


def request_minidom(url, **kwargs):
    """
    Wrapper for `request_response', which will return a Minidom object if no
    exceptions are raised.
    """

    response = request_response(url, **kwargs)

    if response is not None:
        return minidom.parseString(response.content)


def request_json(url, **kwargs):
    """
    Wrapper for `request_response', which will decode the response as JSON
    object and return the result, if no exceptions are raised.

    As an option, a validator callback can be given, which should return True
    if the result is valid.
    """

    validator = kwargs.pop("validator", None)
    response = request_response(url, **kwargs)

    if response is not None:
        try:
            result = response.json()

            if validator and not validator(result):
                logger.error("JSON validation result failed")
            else:
                return result
        except ValueError:
            logger.error("Response returned invalid JSON data")

            # Debug response
            if headphones.VERBOSE:
                server_message(response)


def request_content(url, **kwargs):
    """
    Wrapper for `request_response', which will return the raw content.
    """

    response = request_response(url, **kwargs)

    if response is not None:
        return response.content


def request_feed(url, **kwargs):
    """
    Wrapper for `request_response', which will return a feed object.
    """

    response = request_response(url, **kwargs)

    if response is not None:
        return feedparser.parse(response.content)


def server_message(response):
    """
    Extract server message from response and log in to logger with DEBUG level.

    Some servers return extra information in the result. Try to parse it for
    debugging purpose. Messages are limited to 150 characters, since it may
    return the whole page in case of normal web page URLs
    """

    message = None

    # First attempt is to 'read' the response as HTML
    if response.headers.get("content-type") and \
                    "text/html" in response.headers.get("content-type"):
        try:
            soup = BeautifulSoup(response.content, "html.parser")
        except Exception:
            pass

        # Find body and cleanup common tags to grab content, which probably
        # contains the message.
        message = soup.find("body")
        elements = ("header", "script", "footer", "nav", "input", "textarea")

        for element in elements:

            for tag in soup.find_all(element):
                tag.replaceWith("")

        message = message.text if message else soup.text
        message = message.strip()

    # Second attempt is to just take the response
    if message is None:
        message = response.content.strip()

    if message:
        # Truncate message if it is too long.
        if len(message) > 200:
            if not type(message) == str:
                message = message.decode(headphones.SYS_ENCODING, 'replace')
            message = message[:200] + "..."

        logger.debug("Server responded with message: %s", message)
