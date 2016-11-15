import requests
import json
import datetime
from urllib.parse import parse_qsl
from oauthlib.oauth1 import SIGNATURE_RSA

from collections import OrderedDict
from requests_oauthlib import OAuth1
from django.conf import settings
from django.core.files.base import ContentFile
from django.contrib.contenttypes.models import ContentType

from django.template.defaultfilters import slugify
from taiga.users.models import User
from taiga.projects.models import Project, ProjectTemplate, Membership, Points
from taiga.projects.userstories.models import UserStory, RolePoints
from taiga.projects.tasks.models import Task
from taiga.projects.issues.models import Issue
from taiga.projects.milestones.models import Milestone
from taiga.projects.epics.models import Epic, RelatedUserStory
from taiga.projects.attachments.models import Attachment
from taiga.projects.history.services import take_snapshot
from taiga.projects.history.services import (make_diff_from_dicts,
                                             make_diff_values,
                                             make_key_from_model_object,
                                             get_typename_for_model_class,
                                             FrozenDiff)
from taiga.projects.history.models import HistoryEntry
from taiga.projects.history.choices import HistoryType
from taiga.projects.custom_attributes.models import (UserStoryCustomAttribute,
                                                     TaskCustomAttribute,
                                                     IssueCustomAttribute,
                                                     EpicCustomAttribute)
from taiga.mdrender.service import render as mdrender

EPIC_COLORS = {
    "ghx-label-0": "#ffffff",
    "ghx-label-1": "#815b3a",
    "ghx-label-2": "#f79232",
    "ghx-label-3": "#d39c3f",
    "ghx-label-4": "#3b7fc4",
    "ghx-label-5": "#4a6785",
    "ghx-label-6": "#8eb021",
    "ghx-label-7": "#ac707a",
    "ghx-label-8": "#654982",
    "ghx-label-9": "#f15c75",
}

class JiraClient:
    def __init__(self, server, oauth):
        self.server = server
        self.api_url = server + "/rest/agile/1.0/{}"
        self.main_api_url = server + "/rest/api/2/{}"
        self.oauth = OAuth1(
            oauth['consumer_key'],
            signature_method=SIGNATURE_RSA,
            rsa_key=oauth['key_cert'],
            resource_owner_key=oauth['access_token'],
            resource_owner_secret=oauth['access_token_secret']
        )

    def get(self, uri_path, query_params=None):
        headers = {
            'Content-Type': "application/json"
        }
        if query_params is None:
            query_params = {}

        if uri_path[0] == '/':
            uri_path = uri_path[1:]
        url = self.api_url.format(uri_path)

        response = requests.get(url, params=query_params, headers=headers, auth=self.oauth)

        if response.status_code == 401:
            raise Exception("Unauthorized: %s at %s" % (response.text, url), response)
        if response.status_code != 200:
            raise Exception("Resource Unavailable: %s at %s" % (response.text, url), response)

        return response.json()

    def get_main_api(self, uri_path, query_params=None):
        headers = {
            'Content-Type': "application/json"
        }
        if query_params is None:
            query_params = {}

        if uri_path[0] == '/':
            uri_path = uri_path[1:]
        url = self.main_api_url.format(uri_path)

        response = requests.get(url, params=query_params, headers=headers, auth=self.oauth)

        if response.status_code == 401:
            raise Exception("Unauthorized: %s at %s" % (response.text, url), response)
        if response.status_code != 200:
            raise Exception("Resource Unavailable: %s at %s" % (response.text, url), response)

        return response.json()

    def raw_get(self, absolute_uri, query_params=None):
        if query_params is None:
            query_params = {}

        response = requests.get(absolute_uri, params=query_params, auth=self.oauth)

        if response.status_code == 401:
            raise Exception("Unauthorized: %s at %s" % (response.text, absolute_uri), response)
        if response.status_code != 200:
            raise Exception("Resource Unavailable: %s at %s" % (response.text, absolute_uri), response)

        return response.content



class JiraImporterCommon:
    def __init__(self, user, server, oauth):
        self._user = user
        self._client = JiraClient(server=server, oauth=oauth)

    def list_users(self):
        result = []
        users = self._client.get_main_api("/user/picker", {
            "query": "@",
            "maxResults": 1000,
        })
        for user in users['users']:
            user_data = self._client.get_main_api("/user", {
                "key": user['key']
            })
            result.append({
                "key": user_data['key'],
                "full_name": user_data['displayName'],
                "email": user_data['emailAddress'],
            })
        return result


    def _import_comments(self, obj, issue, options):
        users_bindings = options.get('users_bindings', {})

        for comment in issue['fields']['comment']['comments']:
            snapshot = take_snapshot(
                obj,
                comment=comment['body'],
                user=users_bindings.get(
                    comment['author']['name'],
                    User(full_name=comment['author']['displayName'])
                ),
                delete=False
            )
            HistoryEntry.objects.filter(id=snapshot.id).update(created_at=comment['created'])

        if issue['fields']['comment']['total'] < issue['fields']['comment']['maxResults']:
            offset = len(issue['fields']['comment'])
            while True:
                comments = self._client.get_main_api("/issue/{}/comment".format(issue['key']), {"startAt": offset})
                for comment in comments['values']:
                    snapshot = take_snapshot(
                        obj,
                        comment=comment['body'],
                        user=users_bindings.get(
                            comment['author']['name'],
                            User(full_name=comment['author']['displayName'])
                        ),
                        delete=False
                    )
                    HistoryEntry.objects.filter(id=snapshot.id).update(created_at=comment['created'])

                offset += len(comments['values'])
                if len(comments['values']) <= comments['maxResults']:
                    break

    def _import_attachments(self, obj, issue, options):
        users_bindings = options.get('users_bindings', {})

        for attachment in issue['fields']['attachment']:
            data = self._client.raw_get(attachment['content'])
            att = Attachment(
                owner=users_bindings.get(attachment['author']['name'], self._user),
                project=obj.project,
                content_type=ContentType.objects.get_for_model(obj),
                object_id=obj.id,
                name=attachment['filename'],
                size=attachment['size'],
                created_date=attachment['created'],
                is_deprecated=False,
            )
            att.attached_file.save(attachment['filename'], ContentFile(data), save=True)

    def _import_changelog(self, project, obj, issue, options):
        obj.cummulative_attachments = []
        for history in issue['changelog']['histories']:
            self._import_history(project, obj, history, options)

    def _import_history(self, project, obj, history, options):
        key = make_key_from_model_object(obj)
        typename = get_typename_for_model_class(obj.__class__)
        history_data = self._transform_history_data(project, obj, history, options)
        if history_data is None:
            return

        change_old = history_data['change_old']
        change_new = history_data['change_new']
        hist_type = history_data['hist_type']
        comment = history_data['comment']
        user = history_data['user']

        diff = make_diff_from_dicts(change_old, change_new)
        fdiff = FrozenDiff(key, diff, {})

        values = make_diff_values(typename, fdiff)
        values.update(history_data['update_values'])

        entry = HistoryEntry.objects.create(
            user=user,
            project_id=obj.project.id,
            key=key,
            type=hist_type,
            snapshot=None,
            diff=fdiff.diff,
            values=values,
            comment=comment,
            comment_html=mdrender(obj.project, comment),
            is_hidden=False,
            is_snapshot=False,
        )
        HistoryEntry.objects.filter(id=entry.id).update(created_at=history['created'])
        return HistoryEntry.objects.get(id=entry.id)

    def _transform_history_data(self, project, obj, history, options):
        users_bindings = options.get('users_bindings', {})

        user = {"pk": None, "name": history.get('author', {}).get('displayName', None)}
        taiga_user = users_bindings.get(history.get('author', {}).get('key', None), None)
        if taiga_user:
            user = {"pk": taiga_user.id, "name": taiga_user.get_full_name()}

        result = {
            "change_old": {},
            "change_new": {},
            "update_values": {},
            "hist_type": HistoryType.change,
            "comment": "",
            "user": user
        }

        has_data = False
        for history_item in history['items']:
            if history_item['field'] == "Attachment":
                result['change_old']["attachments"] = []
                for att in obj.cummulative_attachments:
                    result['change_old']["attachments"].append({
                        "id": 0,
                        "filename": att
                    })

                if history_item['from'] is not None:
                    obj.cummulative_attachments.pop(obj.cummulative_attachments.index(history_item['fromString']))
                if history_item['to'] is not None:
                    obj.cummulative_attachments.append(history_item['toString'])

                result['change_new']["attachments"] = []
                for att in obj.cummulative_attachments:
                    result['change_new']["attachments"].append({
                        "id": 0,
                        "filename": att
                    })
                has_data = True
            elif history_item['field'] == "description":
                result['change_old']["description"] = history_item['fromString']
                result['change_new']["description"] = history_item['toString']
                result['change_old']["description_html"] = mdrender(obj.project, history_item['fromString'] or "")
                result['change_new']["description_html"] = mdrender(obj.project, history_item['toString'] or "")
                has_data = True
            elif history_item['field'] == "duedate":
                if isinstance(obj, Task):
                    due_date_field = obj.project.taskcustomattributes.get(name="Due date")
                elif isinstance(obj, UserStory):
                    due_date_field = obj.project.userstorycustomattributes.get(name="Due date")
                elif isinstance(obj, Epic):
                    due_date_field = obj.project.epiccustomattributes.get(name="Due date")

                result['change_old']["custom_attributes"] = [{
                    "name": "Due date",
                    "value": history_item['from'],
                    "id": due_date_field.id
                }]
                result['change_new']["custom_attributes"] = [{
                    "name": "Due date",
                    "value": history_item['to'],
                    "id": due_date_field.id
                }]
                has_data = True
            elif history_item['field'] == "Epic Link":
                pass
            elif history_item['field'] == "labels":
                result['change_old']["tags"] = history_item['fromString'].split()
                result['change_new']["tags"] = history_item['toString'].split()
                has_data = True
            elif history_item['field'] == "Rank":
                pass
            elif history_item['field'] == "RemoteIssueLink":
                pass
            elif history_item['field'] == "Sprint":
                old_milestone = None
                if history_item['fromString']:
                    try:
                        old_milestone = obj.project.milestones.get(name=history_item['fromString']).id
                    except Milestone.DoesNotExist:
                        old_milestone = -1

                new_milestone = None
                if history_item['toString']:
                    try:
                        new_milestone = obj.project.milestones.get(name=history_item['toString']).id
                    except Milestone.DoesNotExist:
                        new_milestone = -2

                result['change_old']["milestone"] = old_milestone
                result['change_new']["milestone"] = new_milestone

                if old_milestone == -1 or new_milestone == -2:
                    result['update_values']["milestone"] = {}

                if old_milestone == -1:
                    result['update_values']["milestone"]["-1"] = history_item['fromString']
                if new_milestone == -2:
                    result['update_values']["milestone"]["-2"] = history_item['toString']
                has_data = True
            elif history_item['field'] == "status":
                if isinstance(obj, Task):
                    result['change_old']["status"] = obj.project.task_statuses.get(name=history_item['fromString']).id
                    result['change_new']["status"] = obj.project.task_statuses.get(name=history_item['toString']).id
                elif isinstance(obj, UserStory):
                    result['change_old']["status"] = obj.project.us_statuses.get(name=history_item['fromString']).id
                    result['change_new']["status"] = obj.project.us_statuses.get(name=history_item['toString']).id
                elif isinstance(obj, Epic):
                    result['change_old']["status"] = obj.project.epic_statuses.get(name=history_item['fromString']).id
                    result['change_new']["status"] = obj.project.epic_statuses.get(name=history_item['toString']).id
                has_data = True
            elif history_item['field'] == "Story Points":
                old_points = None
                if history_item['fromString']:
                    estimation = float(history_item['fromString'])
                    (old_points, _) = Points.objects.get_or_create(
                        project=project,
                        value=estimation,
                        defaults={
                            "name": str(estimation),
                            "order": estimation,
                        }
                    )
                    old_points = old_points.id
                new_points = None
                if history_item['toString']:
                    estimation = float(history_item['toString'])
                    (new_points, _) = Points.objects.get_or_create(
                        project=project,
                        value=estimation,
                        defaults={
                            "name": str(estimation),
                            "order": estimation,
                        }
                    )
                    new_points = new_points.id
                result['change_old']["points"] = {project.roles.get(slug="main").id: old_points}
                result['change_new']["points"] = {project.roles.get(slug="main").id: new_points}
                has_data = True
            elif history_item['field'] == "summary":
                result['change_old']["subject"] = history_item['fromString']
                result['change_new']["subject"] = history_item['toString']
                has_data = True
            elif history_item['field'] == "Epic Color":
                if isinstance(obj, Epic):
                    result['change_old']["color"] = EPIC_COLORS.get(history_item['fromString'], None)
                    result['change_new']["color"] = EPIC_COLORS.get(history_item['toString'], None)
                    Epic.objects.filter(id=obj.id).update(
                        color=EPIC_COLORS.get(history_item['toString'], "#999999")
                    )
                    has_data = True
            elif history_item['field'] == "assignee":
                old_assigned_to = None
                if history_item['from'] is not None:
                    old_assigned_to = users_bindings.get(history_item['from'], -1)
                    if old_assigned_to != -1:
                        old_assigned_to = old_assigned_to.id

                new_assigned_to = None
                if history_item['to'] is not None:
                    new_assigned_to = users_bindings.get(history_item['to'], -2)
                    if new_assigned_to != -2:
                        new_assigned_to = new_assigned_to.id

                result['change_old']["assigned_to"] = old_assigned_to
                result['change_new']["assigned_to"] = new_assigned_to

                if old_assigned_to == -1 or new_assigned_to == -2:
                    result['update_values']["users"] = {}

                if old_assigned_to == -1:
                    result['update_values']["users"]["-1"] = history_item['fromString']
                if new_assigned_to == -2:
                    result['update_values']["users"]["-2"] = history_item['toString']
                has_data = True
            elif history_item['field'] == "priority":
                if isinstance(obj, Task):
                    priority_field = obj.project.taskcustomattributes.get(name="Priority")
                elif isinstance(obj, UserStory):
                    priority_field = obj.project.userstorycustomattributes.get(name="Priority")
                elif isinstance(obj, Epic):
                    priority_field = obj.project.epiccustomattributes.get(name="Priority")

                result['change_old']["custom_attributes"] = [{
                    "name": "Priority",
                    "value": history_item['fromString'],
                    "id": priority_field.id
                }]
                result['change_new']["custom_attributes"] = [{
                    "name": "Priority",
                    "value": history_item['toString'],
                    "id": priority_field.id
                }]
                has_data = True
            else:
                import pprint; pprint.pprint(history_item)

        if not has_data:
            return None

        return result

    @classmethod
    def get_auth_url(cls, server, consumer_key, key_cert_data, verify=None):
        if verify is None:
            verify = server.startswith('https')

        oauth = OAuth1(consumer_key, signature_method=SIGNATURE_RSA, rsa_key=key_cert_data)
        r = requests.post(
            server + '/plugins/servlet/oauth/request-token', verify=verify, auth=oauth)
        request = dict(parse_qsl(r.text))
        request_token = request['oauth_token']
        request_token_secret = request['oauth_token_secret']

        return (
            request_token,
            request_token_secret,
            '{}/plugins/servlet/oauth/authorize?oauth_token={}'.format(server, request_token)
        )

    @classmethod
    def get_access_token(cls, server, consumer_key, key_cert_data, request_token, request_token_secret, verify=False):
        oauth = OAuth1(
            consumer_key,
            signature_method=SIGNATURE_RSA,
            rsa_key=key_cert_data,
            resource_owner_key=request_token,
            resource_owner_secret=request_token_secret
        )
        r = requests.post(server + '/plugins/servlet/oauth/access-token', verify=verify, auth=oauth)
        access = dict(parse_qsl(r.text))

        return {
            'access_token': access['oauth_token'],
            'access_token_secret': access['oauth_token_secret'],
            'consumer_key': consumer_key,
            'key_cert': key_cert_data
        }
