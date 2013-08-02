import logging
import urlparse
from django.middleware.csrf import _get_new_csrf_key

from django.utils.datetime_safe import datetime
logger = logging.getLogger(__name__)

import urllib

from django.conf import settings
from django.http import HttpResponseRedirect, HttpResponsePermanentRedirect, HttpResponseForbidden
from django.shortcuts import redirect
from django.utils import simplejson, translation

_parse_json = lambda s: simplejson.loads(s)

from oauth2 import authenticate, parseSignedRequest
from session import get_session
from graph import get_graph
from modules.profile.application.utils import get_app_dict
from facebook.graph import GraphAPIError
from facebook.modules.profile.application.models import Request as AppRequest


class SignedRequestMiddleware(object):
    def process_request(self, request):
        """
        Writes the signed_request into the Session
        """
        fb = get_session(request)
        setattr(request, 'fb_session', fb)
        application = get_app_dict()

        logger.debug('Request Method = %s\n, AccessToken=%s' % (request.method, fb.access_token))

        if 'deauthorize' in request.path:
            logger.debug('deauthorize call, SignedRequestMiddleware returning...')
            return None
            
        if 'feincms' in settings.INSTALLED_APPS:
            # if feincms is installed, try to get the application from the page
            from facebook.feincms.utils import get_application_from_request
            page_app = get_application_from_request(request)
            if application:
                application = get_app_dict(page_app)


        # default POST request from facebook with a signed request
        if 'signed_request' in request.POST:
            try:
                parsed_request = parseSignedRequest(request.POST['signed_request'], application['SECRET'])
            except ValueError:
                return HttpResponseForbidden()
            logger.debug(u'got signed_request from facebook: %s' % parsed_request)
            # Make sure csrfViewMiddleware lets this pass:
            setattr(request, 'csrf_processing_done', True)
            # but still set a cookie for later use
            request.META["CSRF_COOKIE"] = _get_new_csrf_key()
            cookie_is_new = True

            if 'user' in parsed_request:
                language = parsed_request['user']['locale']
                logger.debug('language: %s' %language)
                request.LANGUAGE_CODE = language
                translation.activate(language)
            fb.signed_request = parsed_request
            logger.debug('stored signed_request')
            expires = None
            # rewrite important data
            if 'oauth_token' in parsed_request:
                expires = datetime.fromtimestamp(float(parsed_request['expires']))
                fb.store_token(parsed_request['oauth_token'], expires)
            elif 'access_token' in parsed_request:
                expires = datetime.fromtimestamp(float(parsed_request['expires']))
                fb.store_token(parsed_request['access_token'], expires)
            else:
                #The chance is good that there is already a valid token in the session. Remove it.
                fb.store_token(None)

            if 'user_id' in parsed_request:
                fb.user_id = parsed_request['user_id']

            else:
                logger.debug("Signed Request didn't contain public user info.")
            if expires:
                logger.debug('Signed Request issued at: %s' % datetime.fromtimestamp(float(parsed_request['issued_at'])))


        # auth via callback from facebook
        elif 'code' in request.GET and 'facebook' in request.META.get('HTTP_REFERER', u''):
            authenticate(request.REQUEST['code'], fb, application,
                         request.build_absolute_uri().split('?')[0] \
                            .replace(application['CANVAS-URL'], application['CANVAS-PAGE']))


    def process_response(self, request, response):
        """ p3p headers for allowing cookies in Internet Explorer.
        more infos: http://adamyoung.net/IE-Blocking-iFrame-Cookies
        thanks to frog32 for the hint """

        response['p3p'] = 'CP="IDC DSP COR ADM DEVi TAIi PSA PSD IVAi IVDi CONi HIS OUR IND CNT"'
        return response


class OAuth2ForCanvasMiddleware(SignedRequestMiddleware):
    #  deprecated old name
    def __init__(self, *args, **kwargs):
        import warnings
        warnings.warn('The name OAuth2ForCanvasMiddleware is deprecated. Use SignedRequestMiddleware instead',
            DeprecationWarning, stacklevel=2)
        super(OAuth2ForCanvasMiddleware, self).__init__(*args, **kwargs)


class Redirect2AppDataMiddleware(object):
    """ If app_data is specified, this middleware assumes that app_data is the deep link and redirects to that page
    example: http://www.facebook.com/PAGENAME?sk=app_APP_ID&app_data=/foo/bar/ redirects to /foo/bar/
    /
    IMPLEMENTATION: this middleware should be placed after OAuth2ForCanvasMiddleware, because it needs session['facebook']
    """

    def process_request(self, request):
        try:
            # only execute first time (Facebook will POST the tab with signed_request parameter)
            if request.method == 'POST' and request.POST.has_key('signed_request'):
                app_data = request.session['facebook']['signed_request']['app_data']

                if '//' in app_data or '..' in app_data:
                    logger.warn('Possible defacing in app_data detected: %s' % app_data)
                    return None

                target_url = '%s%s' % (request.META['SCRIPT_NAME'], app_data)
                logger.debug('got target url: %s' % target_url)
                del request.session['facebook']['signed_request']['app_data']
                request.session.modified = True
                return redirect(target_url)
            else:
                return None
        except KeyError:
            return None

class AppRequestMiddleware(object):
    """ Processes App requests. Generates a Request object for every request
        and attaches it to the session.
        The objects are only stored in the database if DEBUG is True, since
        the ids are available in every request from facebook.
        The app_requests need to be deleted manually from facebook.
    """
    def process_request(self, request):
        app_requests = []
        if request.GET.get('request_ids', None):
            fb = get_session(request)
            request_ids = urllib.unquote(request.GET.get('request_ids'))
            request_ids = request_ids.split(',')
            logger.debug('Got app request ids: %s' % request_ids)
            for id in request_ids:
                r, created = AppRequest.objects.get_or_create(id=int(id))
                if settings.DEBUG and created:
                    try:
                        graph = get_graph(request)
                        r.get_from_facebook(graph, save=True)
                    except GraphAPIError:
                        pass
                app_requests.append(r.id)
            if len(app_requests) > 0:
                fb.app_requests = app_requests


class FakeSessionCookieMiddleware(object):
    # from http://djangosnippets.org/snippets/460/
    def process_request(self, request):
        """ tries to get the session variable via HTTP GET if there is no cookie """
        if not request.COOKIES.has_key(settings.SESSION_COOKIE_NAME) \
            and request.REQUEST.has_key(settings.SESSION_COOKIE_NAME):
            request.COOKIES[settings.SESSION_COOKIE_NAME] = \
              request.REQUEST[settings.SESSION_COOKIE_NAME]
            request.COOKIES['fakesession'] = True

    def process_response(self, request, response):
        cookie_name = settings.SESSION_COOKIE_NAME

        if isinstance(response, (HttpResponseRedirect, HttpResponsePermanentRedirect)):
            location = response._headers['location'][1]

            # only append session id if the redirection stays inside (local)
            if not location.find('http') == 0 and not 'admin' in location:
                separator = '&' if '?' in location else '?'
                response._headers['location'] = ('Location' , '%s%s%s=%s' % (location,
                            separator, cookie_name,
                            request.session._get_session_key()))

                logger.debug('FakeSessionCookieMiddleware: changed redirect location from "%s" to "%s" ' % (location, response._headers['location'][1]))
        return response
