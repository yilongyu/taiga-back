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


class JiraImporter:
    def __init__(self, user, server, oauth):
        self._user = user
        self._client = JiraClient(server=server, oauth=oauth)

    def list_projects(self):
        return self._client.get('/board')

    def list_users(self, project_id):
        project = self._client.get("/board/{}".format(project_id))
        return project

    def import_project(self, project_id, options={"template": "scrum", "users_bindings": {}, "keep_external_reference": False}):
        project = self._import_project_data(project_id, options)
        self._import_epics_data(project_id, project, options)
        self._import_user_stories_data(project_id, project, options)

    def _import_project_data(self, project_id, options):
        project = self._client.get("/board/{}".format(project_id))
        project_config = self._client.get("/board/{}/configuration".format(project_id))
        project_template = ProjectTemplate.objects.get(slug=options['template'])

        project_template.is_epics_activated = True
        project_template.epic_statuses = []
        project_template.us_statuses = []
        project_template.task_statuses = []
        project_template.issue_statuses = []

        counter = 0
        for column in project_config['columnConfig']['columns']:
            project_template.epic_statuses.append({
                "name": column['name'],
                "slug": slugify(column['name']),
                "is_closed": False,
                "is_archived": False,
                "color": "#999999",
                "wip_limit": None,
                "order": counter,
            })
            project_template.us_statuses.append({
                "name": column['name'],
                "slug": slugify(column['name']),
                "is_closed": False,
                "is_archived": False,
                "color": "#999999",
                "wip_limit": None,
                "order": counter,
            })
            project_template.task_statuses.append({
                "name": column['name'],
                "slug": slugify(column['name']),
                "is_closed": False,
                "is_archived": False,
                "color": "#999999",
                "wip_limit": None,
                "order": counter,
            })
            project_template.issue_statuses.append({
                "name": column['name'],
                "slug": slugify(column['name']),
                "is_closed": False,
                "is_archived": False,
                "color": "#999999",
                "wip_limit": None,
                "order": counter,
            })
            counter += 1

        project_template.default_options["epic_status"] = project_template.epic_statuses[0]['name']
        project_template.default_options["us_status"] = project_template.us_statuses[0]['name']
        project_template.default_options["task_status"] = project_template.task_statuses[0]['name']
        project_template.default_options["issue_status"] = project_template.issue_statuses[0]['name']

        project_template.points = [{
            "value": None,
            "name": "?",
            "order": 0,
        }]

        main_permissions = project_template.roles[0]['permissions']
        project_template.roles = [{
            "name": "Main",
            "slug": "main",
            "computable": True,
            "permissions": main_permissions,
            "order": 70,
        }]

        project = Project.objects.create(
            name=project['name'],
            description=project.get('description', ''),
            owner=self._user,
            creation_template=project_template
        )

        for model in [UserStoryCustomAttribute, TaskCustomAttribute, IssueCustomAttribute, EpicCustomAttribute]:
            model.objects.create(
                name="Due date",
                description="Due date",
                type="date",
                order=1,
                project=project
            )
            model.objects.create(
                name="Priority",
                description="Priority",
                type="text",
                order=1,
                project=project
            )

        # for user in options.get('users_bindings', {}).values():
        #     if user != self._user:
        #         Membership.objects.get_or_create(
        #             user=user,
        #             project=project,
        #             role=project.get_roles().get(slug="main"),
        #             is_admin=False,
        #         )
        #
        for sprint in self._client.get("/board/{}/sprint".format(project_id))['values']:
            start_datetime = sprint.get('startDate', None)
            end_datetime = sprint.get('startDate', None)
            start_date = datetime.date.today()
            if start_datetime:
                start_date = start_datetime[:10]
            end_date = datetime.date.today()
            if end_datetime:
                end_date = end_datetime[:10]

            milestone = Milestone.objects.create(
                name=sprint['name'],
                slug=slugify(sprint['name']),
                owner=self._user,
                project=project,
                estimated_start=start_date,
                estimated_finish=end_date,
            )
            Milestone.objects.filter(id=milestone.id).update(
                created_date=start_datetime or datetime.datetime.now(),
                modified_date=start_datetime or datetime.datetime.now(),
            )
        return project

    def _import_user_stories_data(self, project_id, project, options):
        users_bindings = options.get('users_bindings', {})
        due_date_field = project.userstorycustomattributes.get(name="Due date")
        priority_field = project.userstorycustomattributes.get(name="Priority")
        project_conf = self._client.get("/board/{}/configuration".format(project_id))
        estimation_field = project_conf['estimation']['field']['fieldId']

        counter = 0
        offset = 0
        while True:
            issues = self._client.get("/board/{}/issue".format(project_id), {
                "startAt": offset,
                "expand": "changelog",
            })
            offset += issues['maxResults']

            for issue in issues['issues']:
                assigned_to = users_bindings.get(issue['fields']['assignee']['key'] if issue['fields']['assignee'] else None, None)
                owner = users_bindings.get(issue['fields']['creator']['key'] if issue['fields']['creator'] else None, self._user)

                external_reference = None
                if options.get('keep_external_reference', False):
                    external_reference = ["jira", issue['fields']['url']]

                try:
                    milestone = project.milestones.get(name=issue['fields'].get('sprint', {}).get('name', ''))
                except Milestone.DoesNotExist:
                    milestone = None

                us = UserStory.objects.create(
                    project=project,
                    owner=owner,
                    assigned_to=assigned_to,
                    status=project.us_statuses.get(name=issue['fields']['status']['name']),
                    kanban_order=counter,
                    sprint_order=counter,
                    backlog_order=counter,
                    subject=issue['fields']['summary'],
                    description=issue['fields']['description'] or '',
                    tags=issue['fields']['labels'],
                    external_reference=external_reference,
                    milestone=milestone,
                )

                try:
                    epic = project.epics.get(ref=int(issue['fields'].get("epic", {}).get("key", "FAKE-0").split("-")[1]))
                    RelatedUserStory.objects.create(
                        user_story=us,
                        epic=epic,
                        order=1
                    )
                except Epic.DoesNotExist:
                    pass

                estimation = None
                if issue['fields'].get(estimation_field, None):
                    estimation = float(issue['fields'].get(estimation_field))

                (points, _) = Points.objects.get_or_create(
                    project=project,
                    value=estimation,
                    defaults={
                        "name": str(estimation),
                        "order": estimation,
                    }
                )
                RolePoints.objects.filter(user_story=us, role__slug="main").update(points_id=points.id)

                if issue['fields']['duedate'] or issue['fields']['priority']:
                    custom_attributes_values = {}
                    if issue['fields']['duedate']:
                        custom_attributes_values[due_date_field.id] = issue['fields']['duedate']
                    if issue['fields']['priority']:
                        custom_attributes_values[priority_field.id] = issue['fields']['priority']['name']
                    us.custom_attributes_values.attributes_values = custom_attributes_values
                    us.custom_attributes_values.save()

                us.ref = issue['key'].split("-")[1]
                UserStory.objects.filter(id=us.id).update(
                    ref=us.ref,
                    modified_date=issue['fields']['updated'],
                    created_date=issue['fields']['created']
                )
                take_snapshot(us, comment="", user=None, delete=False)
                self._import_subtasks(project_id, project, us, issue, options)
                self._import_comments(us, issue, options)
                self._import_attachments(us, issue, options)
                self._import_changelog(project, us, issue, options)
                counter += 1

            if len(issues['issues']) < issues['maxResults']:
                break

    def _import_subtasks(self, project_id, project, us, issue, options):
        users_bindings = options.get('users_bindings', {})
        due_date_field = project.taskcustomattributes.get(name="Due date")
        priority_field = project.taskcustomattributes.get(name="Priority")

        if len(issue['fields']['subtasks']) == 0:
            return

        counter = 0
        offset = 0
        while True:
            issues = self._client.get("/board/{}/issue".format(project_id), {
                "jql": "parent={}".format(issue['key']),
                "startAt": offset,
                "expand": "changelog",
            })
            offset += issues['maxResults']

            for issue in issues['issues']:
                assigned_to = users_bindings.get(issue['fields']['assignee']['key'] if issue['fields']['assignee'] else None, None)
                owner = users_bindings.get(issue['fields']['creator']['key'] if issue['fields']['creator'] else None, self._user)

                external_reference = None
                if options.get('keep_external_reference', False):
                    external_reference = ["jira", issue['fields']['url']]

                task = Task.objects.create(
                    user_story=us,
                    project=project,
                    owner=owner,
                    assigned_to=assigned_to,
                    status=project.task_statuses.get(name=issue['fields']['status']['name']),
                    subject=issue['fields']['summary'],
                    description=issue['fields']['description'] or '',
                    tags=issue['fields']['labels'],
                    external_reference=external_reference,
                    milestone=us.milestone,
                )

                if issue['fields']['duedate'] or issue['fields']['priority']:
                    custom_attributes_values = {}
                    if issue['fields']['duedate']:
                        custom_attributes_values[due_date_field.id] = issue['fields']['duedate']
                    if issue['fields']['priority']:
                        custom_attributes_values[priority_field.id] = issue['fields']['priority']['name']
                    task.custom_attributes_values.attributes_values = custom_attributes_values
                    task.custom_attributes_values.save()

                task.ref = issue['key'].split("-")[1]
                Task.objects.filter(id=task.id).update(
                    ref=task.ref,
                    modified_date=issue['fields']['updated'],
                    created_date=issue['fields']['created']
                )
                take_snapshot(task, comment="", user=None, delete=False)
                for subtask in issue['fields']['subtasks']:
                    print("WARNING: Ignoring subtask {} because parent isn't a User Story".format(subtask['key']))
                self._import_comments(task, issue, options)
                self._import_attachments(task, issue, options)
                self._import_changelog(project, task, issue, options)
                counter += 1
            if len(issues['issues']) < issues['maxResults']:
                break

    def _import_epics_data(self, project_id, project, options):
        users_bindings = options.get('users_bindings', {})
        due_date_field = project.epiccustomattributes.get(name="Due date")
        priority_field = project.epiccustomattributes.get(name="Priority")

        counter = 0
        offset = 0
        while True:
            issues = self._client.get("/board/{}/epic".format(project_id), {
                "startAt": offset,
            })
            offset += issues['maxResults']

            for epic in issues['values']:
                issue = self._client.get("/issue/{}".format(epic['key']))
                assigned_to = users_bindings.get(issue['fields']['assignee']['key'] if issue['fields']['assignee'] else None, None)
                owner = users_bindings.get(issue['fields']['creator']['key'] if issue['fields']['creator'] else None, self._user)

                external_reference = None
                if options.get('keep_external_reference', False):
                    external_reference = ["jira", issue['fields']['url']]

                epic = Epic.objects.create(
                    project=project,
                    owner=owner,
                    assigned_to=assigned_to,
                    status=project.epic_statuses.get(name=issue['fields']['status']['name']),
                    subject=issue['fields']['summary'],
                    description=issue['fields']['description'] or '',
                    epics_order=counter,
                    tags=issue['fields']['labels'],
                    external_reference=external_reference,
                )

                if issue['fields']['duedate'] or issue['fields']['priority']:
                    custom_attributes_values = {}
                    if issue['fields']['duedate']:
                        custom_attributes_values[due_date_field.id] = issue['fields']['duedate']
                    if issue['fields']['priority']:
                        custom_attributes_values[priority_field.id] = issue['fields']['priority']['name']
                    epic.custom_attributes_values.attributes_values = custom_attributes_values
                    epic.custom_attributes_values.save()

                epic.ref = issue['key'].split("-")[1]
                Epic.objects.filter(id=epic.id).update(
                    ref=epic.ref,
                    modified_date=issue['fields']['updated'],
                    created_date=issue['fields']['created']
                )

                take_snapshot(epic, comment="", user=None, delete=False)
                self._import_attachments(epic, issue, options)
                for subtask in issue['fields']['subtasks']:
                    print("WARNING: Ignoring subtask {} because parent isn't a User Story".format(subtask['key']))
                self._import_comments(epic, issue, options)
                self._import_attachments(epic, issue, options)
                issue_with_changelog = self._client.get("/issue/{}".format(issue['key']), {
                    "expand": "changelog"
                })
                self._import_changelog(project, epic, issue_with_changelog, options)
                counter += 1

            if len(issues['values']) < issues['maxResults']:
                break

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
            if isinstance(obj, Epic):
                import pprint; pprint.pprint(history_item)
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
