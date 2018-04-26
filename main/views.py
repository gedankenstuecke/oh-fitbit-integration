import logging
import requests
import os
import base64
import json
import arrow
import tempfile

from requests_respectful import (RespectfulRequester,
                                 RequestsRespectfulRateLimitedError)
from django.contrib.auth import login
from django.shortcuts import render, redirect
from django.conf import settings
from datauploader.tasks import xfer_to_open_humans
from urllib.parse import parse_qs
from open_humans.models import OpenHumansMember
from .models import FitbitMember
from ohapi import api


# Set up logging.
logger = logging.getLogger(__name__)

# Fitbit settings
fitbit_authorize_url = 'https://www.fitbit.com/oauth2/authorize'
fitbit_token_url = 'https://api.fitbit.com/oauth2/token'

if settings.REMOTE is True:
    from urllib.parse import urlparse
    url_object = urlparse(os.getenv('REDIS_URL'))
    logger.info('Connecting to redis at %s:%s',
        url_object.hostname,
        url_object.port)
    RespectfulRequester.configure(
        redis={
            "host": url_object.hostname,
            "port": url_object.port,
            "password": url_object.password,
            "database": 0
        },
        safety_threshold=5)

# Requests Respectful (rate limiting, waiting)
rr = RespectfulRequester()
rr.register_realm("Fitbit", max_requests=3600, timespan=3600)


def index(request):
    """
    Starting page for app.
    """

    context = {'client_id': settings.OPENHUMANS_CLIENT_ID,
               'oh_proj_page': settings.OH_ACTIVITY_PAGE}

    return render(request, 'main/index.html', context=context)


def complete_fitbit(request):

    code = request.GET['code']

    # Create Base64 encoded string of clientid:clientsecret for the headers for Fitbit
    # https://dev.fitbit.com/build/reference/web-api/oauth2/#access-token-request
    encode_fitbit_auth = str(settings.FITBIT_CLIENT_ID) + ":" + str(settings.FITBIT_CLIENT_SECRET)
    print(encode_fitbit_auth)
    b64header = base64.b64encode(encode_fitbit_auth.encode("UTF-8")).decode("UTF-8")
    # Add the payload of code and grant_type. Construct headers
    payload = {'code': code, 'grant_type': 'authorization_code'}
    headers = {'Content-Type': 'application/x-www-form-urlencoded', 'Authorization': 'Basic %s' % (b64header)}
    # Make request for access token
    r = requests.post(fitbit_token_url, payload, headers=headers)
    # print(r.json())

    rjson = r.json()

    oh_id = request.user.oh_member.oh_id
    oh_user = OpenHumansMember.objects.get(oh_id=oh_id)

    # Save the user as a FitbitMember and store tokens
    try:
        fitbit_member = FitbitMember.objects.get(userid=rjson['user_id'])
        fitbit_member.access_token = rjson['access_token']
        fitbit_member.refresh_token = rjson['refresh_token']
        fitbit_member.expires_in = rjson['expires_in']
        fitbit_member.scope = rjson['scope']
        fitbit_member.token_type = rjson['token_type']
        fitbit_member.save()
    except:
        fitbit_member = FitbitMember.objects.get_or_create(
            user=oh_user,
            userid=rjson['user_id'],
            access_token=rjson['access_token'],
            refresh_token=rjson['refresh_token'],
            expires_in=rjson['expires_in'],
            scope=rjson['scope'],
            token_type=rjson['token_type'])

    # Fetch user's existing data from OH
    # We are going to use the pip package open-humans-api for this  
    fitbit_data = get_existing_fitbit(oh_user.access_token)
    # print(fitbit_data)

    # Fetch user's data from Fitbit (update the data if it already existed)
    alldata = fetch_fitbit_data(fitbit_member, rjson['access_token'], fitbit_data)

    # metadata = {
    #     'tags': ['fitbit', 'tracker', 'activity'],
    #     'description': 'File with Fitbit data',
    # }

    # xfer_to_open_humans.delay(alldata, metadata, oh_id=oh_id)

    context = {'oh_proj_page': settings.OH_ACTIVITY_PAGE}
    return render(request, 'main/complete.html',
                  context=context)

def get_existing_fitbit(oh_access_token):
    member = api.exchange_oauth2_member(oh_access_token)
    for dfile in member['data']:
        if 'fitbit' in dfile['metadata']['tags']:
            # get file here and read the json into memory
            tf_in = tempfile.NamedTemporaryFile(suffix='.json')
            tf_in.write(requests.get(dfile['download_url']).content)
            tf_in.flush()
            fitbit_data = json.load(open(tf_in.name))
            return fitbit_data
    return []

class RateLimitException(Exception):
    """
    An exception that is raised if we reach a request rate cap.
    """

    # TODO: add the source of the rate limit we hit for logging (fitit,
    # internal global fitbit, internal user-specific fitbit)

    pass


def fetch_fitbit_data(fitbit_member, access_token, fitbit_data=None):
    '''
    Fetches all of the fitbit data for a given user
    '''
    fitbit_urls = [
        # Requires the 'settings' scope, which we haven't asked for
        # {'name': 'devices', 'url': '/-/devices.json', 'period': None},

        {'name': 'activities-overview',
         'url': '/{user_id}/activities.json',
         'period': None},

        # interday timeline data
        {'name': 'heart',
         'url': '/{user_id}/activities/heart/date/{start_date}/{end_date}.json',
         'period': 'month'},
        # MPB 2016-12-12: Although docs allowed for 'year' for this endpoint,
        # switched to 'month' bc/ req for full year started resulting in 504.
        {'name': 'tracker-activity-calories',
         'url': '/{user_id}/activities/tracker/activityCalories/date/{start_date}/{end_date}.json',
         'period': 'month'},
        {'name': 'tracker-calories',
         'url': '/{user_id}/activities/tracker/calories/date/{start_date}/{end_date}.json',
         'period': 'year'},
        {'name': 'tracker-distance',
         'url': '/{user_id}/activities/tracker/distance/date/{start_date}/{end_date}.json',
         'period': 'year'},
        {'name': 'tracker-elevation',
         'url': '/{user_id}/activities/tracker/elevation/date/{start_date}/{end_date}.json',
         'period': 'year'},
        {'name': 'tracker-floors',
         'url': '/{user_id}/activities/tracker/floors/date/{start_date}/{end_date}.json',
         'period': 'year'},
        {'name': 'tracker-minutes-fairly-active',
         'url': '/{user_id}/activities/tracker/minutesFairlyActive/date/{start_date}/{end_date}.json',
         'period': 'year'},
        {'name': 'tracker-minutes-lightly-active',
         'url': '/{user_id}/activities/tracker/minutesLightlyActive/date/{start_date}/{end_date}.json',
         'period': 'year'},
        {'name': 'tracker-minutes-sedentary',
         'url': '/{user_id}/activities/tracker/minutesSedentary/date/{start_date}/{end_date}.json',
         'period': 'year'},
        {'name': 'tracker-minutes-very-active',
         'url': '/{user_id}/activities/tracker/minutesVeryActive/date/{start_date}/{end_date}.json',
         'period': 'year'},
        {'name': 'tracker-steps',
         'url': '/{user_id}/activities/tracker/steps/date/{start_date}/{end_date}.json',
         'period': 'year'},
        {'name': 'weight-log',
         'url': '/{user_id}/body/log/weight/date/{start_date}/{end_date}.json',
         'period': 'month'},
        {'name': 'weight',
         'url': '/{user_id}/body/weight/date/{start_date}/{end_date}.json',
         'period': 'year'},
        {'name': 'sleep-awakenings',
         'url': '/{user_id}/sleep/awakeningsCount/date/{start_date}/{end_date}.json',
         'period': 'year'},
        {'name': 'sleep-efficiency',
         'url': '/{user_id}/sleep/efficiency/date/{start_date}/{end_date}.json',
         'period': 'year'},
        {'name': 'sleep-minutes-after-wakeup',
         'url': '/{user_id}/sleep/minutesAfterWakeup/date/{start_date}/{end_date}.json',
         'period': 'year'},
        {'name': 'sleep-minutes',
         'url': '/{user_id}/sleep/minutesAsleep/date/{start_date}/{end_date}.json',
         'period': 'year'},
        {'name': 'awake-minutes',
         'url': '/{user_id}/sleep/minutesAwake/date/{start_date}/{end_date}.json',
         'period': 'year'},
        {'name': 'minutes-to-sleep',
         'url': '/{user_id}/sleep/minutesToFallAsleep/date/{start_date}/{end_date}.json',
         'period': 'year'},
        {'name': 'sleep-start-time',
         'url': '/{user_id}/sleep/startTime/date/{start_date}/{end_date}.json',
         'period': 'year'},
        {'name': 'time-in-bed',
         'url': '/{user_id}/sleep/timeInBed/date/{start_date}/{end_date}.json',
         'period': 'year'},
    ]

    # Set up user realm since rate limiting is per-user
    print(fitbit_member.user)
    user_realm = 'fitbit-{}'.format(fitbit_member.user.oh_id)
    rr.register_realm(user_realm, max_requests=150, timespan=3600)
    rr.update_realm(user_realm, max_requests=150, timespan=3600)

    # Get initial information about user from Fitbit
    headers = {'Authorization': "Bearer %s" % access_token}
    query_result = requests.get('https://api.fitbit.com/1/user/-/profile.json', headers=headers).json()

    # Store the user ID since it's used in all future queries
    user_id = query_result['user']['encodedId']
    member_since = query_result['user']['memberSince']
    start_date = arrow.get(member_since, 'YYYY-MM-DD')

    # Refresh token if the result is 401
    # TODO: update this so it just checks the expired field.
    # if query_result.status_code == 401:
    #     fitbit_member._refresh_tokens()

    # Reset data if user account ID has changed.
    if 'profile' in fitbit_data:
        if fitbit_data['profile']['encodedId'] != user_id:
            logging.info(
                'User ID changed from {} to {}. Resetting all data.'.format(
                    fitbit_data['profile']['encodedId'], user_id))
            fitbit_data = defaultdict(dict)
        else:
            logging.debug('User ID ({}) matches old data.'.format(user_id))

    fitbit_data['profile'] = {
        'averageDailySteps': query_result['user']['averageDailySteps'],
        'encodedId': user_id,
        'height': query_result['user']['height'],
        'memberSince': member_since,
        'strideLengthRunning': query_result['user']['strideLengthRunning'],
        'strideLengthWalking': query_result['user']['strideLengthWalking'],
        'weight': query_result['user']['weight'],
    }

    # Some block about if the period is none
    for url in [u for u in fitbit_urls if u['period'] is None]:
        if not user_id and 'profile' in fitbit_data:
            user_id = fitbit_data['profile']['user']['encodedId']

        # Build URL
        fitbit_api_base_url = 'https://api.fitbit.com/1/user'
        final_url = fitbit_api_base_url + url['url'].format(user_id=user_id)
        # Fetch the data
        try:
            print(final_url)
            r = rr.get(url=final_url, 
                    headers=headers, 
                    realms=["Fitbit", 'fitbit-{}'.format(fitbit_member.user.oh_id)])
        except RequestsRespectfulRateLimitedError:
            logging.info('Requests-respectful reports rate limit hit.')
            raise RateLimitException()

        fitbit_data[url['name']] = r

    # Loop over URLs, format with user info.
    # results = {}
    # for url in fitbit_urls:
    #     fitbit_api_base_url = 'https://api.fitbit.com/1/user'
    #     final_url = fitbit_api_base_url + url['url'].format(user_id=user_id,
    #                                                         start_date=start_date,
    #                                                         end_date=arrow.utcnow().format('YYYY-MM-DD'))
        # Fetch the data
        # try:
        #     r = rr.get(url=final_url, 
        #             headers=headers, 
        #             realms=["Fitbit", 'fitbit-{}'.format(fitbit_member.oh_member.oh_id)])
        #     # Append the results to results dictionary with url "name" as the key
        #     results[url['name']] = r.json()
        # except RequestsRespectfulRateLimitedError:
        #     logging.info('Requests-respectful reports rate limit hit.')
        #     raise RateLimitException()
            # Moves integration
            # print('requeued processing with 60 secs delay')
            # process_moves.apply_async((oh_member.oh_id), countdown=61)

    #Period year URLs
    for url in [u for u in fitbit_urls if u['period'] == 'year']:
        years = arrow.Arrow.range('year', start_date.floor('year'),
                                arrow.get())
        for year_date in years:
            year = year_date.format('YYYY')

            if year in fitbit_data[url['name']]:
                logger.info('Skip retrieval {}: {}'.format(url['name'], year))
                continue

            logger.info('Retrieving %s: %s', url['name'], year)
            # Build URL
            fitbit_api_base_url = 'https://api.fitbit.com/1/user'
            final_url = fitbit_api_base_url + url['url'].format(user_id=user_id,
                                                                start_date=year_date.floor('year').format('YYYY-MM-DD'),
                                                                end_date=year_date.ceil('year').format('YYYY-MM-DD'))
            # Fetch the data
            try:
                print(final_url)
                r = rr.get(url=final_url, 
                        headers=headers, 
                        realms=["Fitbit", 'fitbit-{}'.format(fitbit_member.user.oh_id)])
            except RequestsRespectfulRateLimitedError:
                logging.info('Requests-respectful reports rate limit hit.')
                raise RateLimitException()

            fitbit_data[url['name']][str(year)] = r

    for url in [u for u in fitbit_urls if u['period'] == 'month']:
        months = arrow.Arrow.range('month', start_date.floor('month'),
                                arrow.get())
        for month_date in months:
            month = month_date.format('YYYY-MM')

            if month in fitbit_data[url['name']]:
                logger.info('Skip retrieval {}: {}'.format(url['name'], month))
                continue

            logger.info('Retrieving %s: %s', url['name'], month)
            # Build URL
            fitbit_api_base_url = 'https://api.fitbit.com/1/user'
            final_url = fitbit_api_base_url + url['url'].format(user_id=user_id,
                                                                start_date=month_date.floor('month').format('YYYY-MM-DD'),
                                                                end_date=month_date.ceil('month').format('YYYY-MM-DD'))
            # Fetch the data
            try:
                print(final_url)
                r = rr.get(url=final_url, 
                        headers=headers, 
                        realms=["Fitbit", 'fitbit-{}'.format(fitbit_member.user.oh_id)])
            except RequestsRespectfulRateLimitedError:
                logging.info('Requests-respectful reports rate limit hit.')
                print('Requests-respectful reports rate limit hit.')
                print(r.text)
                raise RateLimitException()

            fitbit_data[url['name']][month] = r

    print(fitbit_data)
    return fitbit_data



def complete(request):
    """
    Receive user from Open Humans. Store data, start upload.
    """
    logger.debug("Received user returning from Open Humans.")
    # Exchange code for token.
    # This creates an OpenHumansMember and associated user account.
    code = request.GET.get('code', '')
    oh_member = oh_code_to_member(code=code)

    if oh_member:
        # Log in the user.
        user = oh_member.user
        login(request, user,
              backend='django.contrib.auth.backends.ModelBackend')

        auth_url = 'https://www.fitbit.com/oauth2/authorize?response_type=code&client_id='+settings.FITBIT_CLIENT_ID+'&scope=activity%20nutrition%20heartrate%20location%20nutrition%20profile%20settings%20sleep%20social%20weight'

        context = {'oh_id': oh_member.oh_id,
                   'oh_proj_page': settings.OH_ACTIVITY_PAGE,
                   'authorization_url': auth_url}
        return render(request, 'main/fitbit.html',
                      context=context)

    logger.debug('Invalid code exchange. User returned to starting page.')
    return redirect('/')


def oh_code_to_member(code):
    """
    Exchange code for token, use this to create and return OpenHumansMember.
    If a matching OpenHumansMember exists, update and return it.
    """
    if settings.OPENHUMANS_CLIENT_SECRET and \
       settings.OPENHUMANS_CLIENT_ID and code:
        data = {
            'grant_type': 'authorization_code',
            'redirect_uri':
            '{}/complete/oh'.format(settings.OPENHUMANS_APP_BASE_URL),
            'code': code,
        }
        req = requests.post(
            '{}/oauth2/token/'.format(settings.OPENHUMANS_OH_BASE_URL),
            data=data,
            auth=requests.auth.HTTPBasicAuth(
                settings.OPENHUMANS_CLIENT_ID,
                settings.OPENHUMANS_CLIENT_SECRET
            )
        )
        data = req.json()

        if 'access_token' in data:
            oh_id = oh_get_member_data(
                data['access_token'])['project_member_id']
            try:
                oh_member = OpenHumansMember.objects.get(oh_id=oh_id)
                logger.debug('Member {} re-authorized.'.format(oh_id))
                oh_member.access_token = data['access_token']
                oh_member.refresh_token = data['refresh_token']
                oh_member.token_expires = OpenHumansMember.get_expiration(
                    data['expires_in'])
            except OpenHumansMember.DoesNotExist:
                oh_member = OpenHumansMember.create(
                    oh_id=oh_id,
                    access_token=data['access_token'],
                    refresh_token=data['refresh_token'],
                    expires_in=data['expires_in'])
                logger.debug('Member {} created.'.format(oh_id))
            oh_member.save()

            return oh_member

        elif 'error' in req.json():
            logger.debug('Error in token exchange: {}'.format(req.json()))
        else:
            logger.warning('Neither token nor error info in OH response!')
    else:
        logger.error('OH_CLIENT_SECRET or code are unavailable')
    return None


def oh_get_member_data(token):
    """
    Exchange OAuth2 token for member data.
    """
    req = requests.get(
        '{}/api/direct-sharing/project/exchange-member/'
        .format(settings.OPENHUMANS_OH_BASE_URL),
        params={'access_token': token}
        )
    if req.status_code == 200:
        return req.json()
    raise Exception('Status code {}'.format(req.status_code))
    return None
