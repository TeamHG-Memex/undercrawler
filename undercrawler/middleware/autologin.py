from http.cookies import SimpleCookie, CookieError
from urllib.parse import urljoin
import json
import logging
import time

import requests
from scrapy.exceptions import IgnoreRequest, NotConfigured


logger = logging.getLogger(__name__)


class AutologinMiddleware:
    '''
    Autologin middleware uses autologin to make all requests while being
    logged in. It uses autologin to get cookies, detects logouts and tries
    to avoid them in the future.

    Required settings:
    AUTOLOGIN_ENABLED = True
    AUTOLOGIN_URL: url of where the autologin service is running
    COOKIES_ENABLED = False (this could be relaxed perhaps)
    AUTH_COOKIES: optionally, pass auth cookies after manual login
    (format is "name=value; name2=value2")
    LOGOUT_URL: optionally, pass url substring to avoid

    We assume a single domain in the whole process here.
    To relax this assumption, following fixes are required:
    - make all state in AutologinMiddleware be domain dependant
    - do not block event loop in login() method (instead, collect
    scheduled requests in a separate queue and make request with scrapy).
    '''
    def __init__(self, autologin_url, auth_cookies=None, logout_url=None,
                 splash_url=None):
        self.autologin_url = autologin_url
        self.splash_url = splash_url
        if auth_cookies:
            cookies = SimpleCookie()
            cookies.load(auth_cookies)
            self.auth_cookies = {m.key: m.value for m in cookies.values()}
            self.logged_in = True
        else:
            self.auth_cookies = None
            self.logged_in = False
        self.logout_urls = set()
        if logout_url:
            self.logout_urls.add(logout_url)

    @classmethod
    def from_crawler(cls, crawler):
        if not crawler.settings.getbool('AUTOLOGIN_ENABLED'):
            raise NotConfigured
        return cls(
            autologin_url=crawler.settings.get('AUTOLOGIN_URL'),
            auth_cookies=crawler.settings.get('AUTH_COOKIES'),
            logout_url=crawler.settings.get('LOGOUT_URL'),
            splash_url=crawler.settings.get('SPLASH_URL'),
            )

    def process_request(self, request, spider):
        ''' Login if we are not logged in yet.
        '''
        if '_autologin' in request.meta:
            return
        # Save original request to be able to retry it in case of logout
        req_copy = request.copy()
        req_copy.callback = req_copy.errback = None
        request.meta['_autologin'] = {'request': req_copy}

        if not self.logged_in:
            self.auth_cookies = self.get_cookies(request.url)
            self.logged_in = True
        elif any(url in request.url for url in self.logout_urls):
            logger.debug('Ignoring logout request %s', request.url)
            raise IgnoreRequest

        # Do our own "cookie" management, so that changes in self.auth_cookies
        # are applied to all future requests immediately.
        if self.auth_cookies:
            request.headers.pop('cookie', None)
            request.headers['cookie'] = '; '.join(
                '{}={}'.format(k, v) for k, v in self.auth_cookies.items())
            request.meta['_autologin']['cookies'] = dict(self.auth_cookies)

    def get_cookies(self, url):
        logger.debug('Attempting login at %s', url)
        while True:
            request = requests.post(
                urljoin(self.autologin_url, '/login-cookies'),
                data=json.dumps({
                    'url': url,
                    'splash_url': self.splash_url,
                }),
                headers={'content-type': 'application/json'})
            response = request.json()
            status = response['status']
            logger.debug('Got login response with status "%s"', status)
            if status == 'pending':
                time.sleep(1.0)
                continue
            elif status == 'skipped':
                return None
            elif status == 'solved':
                cookies = response.get('cookies')
                if cookies:
                    cookie_dict = {c['name']: c['value'] for c in cookies}
                    logger.debug('Got cookies after login %s', cookie_dict)
                    return cookie_dict
                else:
                    logger.debug('No cookies after login')
                    return None

    def process_response(self, request, response, spider):
        ''' If we were logged out, login again and retry request.
        '''
        if self.is_logout(response):
            logger.debug('Logout at %s %s',
                         response.url, response.headers.getlist('set-cookie'))
            autologin_meta = request.meta['_autologin']
            # We could have already done relogin after initial logout
            if any(autologin_meta['cookies'].get(k) != v
                    for k, v in self.auth_cookies.items()):
                retryreq = autologin_meta['request'].copy()
                retryreq.dont_filter = True
                logger.debug('Stale request %s was logged out, will retry %s',
                             response, retryreq)
                return retryreq
            logger.debug('Logged out at %s, will retry login', response.url)
            self.auth_cookies = self.get_cookies(response.url)
            # TODO - could have been an expired session, do not judge too early
            self.logout_urls.add(response.url)
            raise IgnoreRequest
        return response

    def is_logout(self, response):
        return (
            self.auth_cookies and
            any(self.auth_cookies.get(name) and m.value == ''
                for name, m in get_cookies_from_header(
                    response, 'set-cookie').items()))


def get_cookies_from_header(response, header_name):
    cookies = SimpleCookie()
    for set_cookie in response.headers.getlist(header_name):
        try:
            cookies.load(set_cookie.decode('utf-8'))
        except CookieError:
            pass
    return cookies
