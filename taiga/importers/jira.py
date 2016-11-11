import requests
import json
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


class JiraClient:
    def __init__(self, server, oauth):
        self.server = server
        self.api_url = server + "/rest/api/2/{}"
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


class JiraImporter:
    def __init__(self, user, server, oauth):
        self._user = user
        print(json.dumps(oauth))
        self._client = JiraClient(server=server, oauth=oauth)

    def list_projects(self):
        return self._client.get('/project')

    def list_users(self, project_id):
        project = self._client.get("/project/{}".format(project_id))
        return project['components']

    def list_issue_types(self, project_id):
        statuses = self._client.get("/project/{}/statuses".format(project_id))
        return statuses

    def import_project(self, project_id, options={"template": "scrum", "users_bindings": {}, "keep_external_reference": False}):
        project = self._import_project_data(project_id, options)
        self._import_user_stories_data(project_id, project, options)
        self._import_epics_data(project_id, project, options)
        self._link_epics_with_user_stories(project_id, project, options)
        self._import_issues_data(project_id, project, options)

    def _import_project_data(self, project_id, options):
        project = self._client.get("/project/{}".format(project_id))
        project_template = ProjectTemplate.objects.get(slug=options['template'])

        epic_statuses = OrderedDict()
        for issue_type in options.get('types_bindings', {}).get("epic", []):
            for status in issue_type['statuses']:
                epic_statuses[status['name']] = status

        us_statuses = OrderedDict()
        for issue_type in options.get('types_bindings', {}).get("us", []):
            for status in issue_type['statuses']:
                us_statuses[status['name']] = status

        task_statuses = OrderedDict()
        for issue_type in options.get('types_bindings', {}).get("task", []):
            for status in issue_type['statuses']:
                task_statuses[status['name']] = status

        issue_statuses = OrderedDict()
        for issue_type in options.get('types_bindings', {}).get("issue", []):
            for status in issue_type['statuses']:
                issue_statuses[status['name']] = status

        counter = 0
        if epic_statuses:
            project_template.epic_statuses = []
            project_template.is_epics_activated = True
        for epic_status in epic_statuses.values():
            project_template.epic_statuses.append({
                "name": epic_status['name'],
                "slug": slugify(epic_status['name']),
                "is_closed": False,
                "is_archived": False,
                "color": "#999999",
                "wip_limit": None,
                "order": counter,
            })
            counter += 1
        if epic_statuses:
            project_template.default_options["epic_status"] = list(epic_statuses.values())[0]['name']

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

        counter = 0
        if us_statuses:
            project_template.us_statuses = []
        for us_status in us_statuses.values():
            project_template.us_statuses.append({
                "name": us_status['name'],
                "slug": slugify(us_status['name']),
                "is_closed": False,
                "is_archived": False,
                "color": "#999999",
                "wip_limit": None,
                "order": counter,
            })
            counter += 1
        if us_statuses:
            project_template.default_options["us_status"] = list(us_statuses.values())[0]['name']

        counter = 0
        if task_statuses:
            project_template.task_statuses = []
        for task_status in task_statuses.values():
            project_template.task_statuses.append({
                "name": task_status['name'],
                "slug": slugify(task_status['name']),
                "is_closed": False,
                "is_archived": False,
                "color": "#999999",
                "wip_limit": None,
                "order": counter,
            })
            counter += 1
        if task_statuses:
            project_template.default_options["task_status"] = list(task_statuses.values())[0]['name']

        counter = 0
        if issue_statuses:
            project_template.issue_statuses = []
        for issue_status in issue_statuses.values():
            project_template.issue_statuses.append({
                "name": issue_status['name'],
                "slug": slugify(issue_status['name']),
                "is_closed": False,
                "is_archived": False,
                "color": "#999999",
                "wip_limit": None,
                "order": counter,
            })
            counter += 1
        if issue_statuses:
            project_template.default_options["issue_status"] = list(issue_statuses.values())[0]['name']


        # main_permissions = project_template.roles[0]['permissions']
        # project_template.roles = []
        # for role in self._client.get("/project/{}/role".format(project_id)).keys():
        #     project_template.roles = [{
        #         "name": role,
        #         "slug": slugify(role),
        #         "computable": True,
        #         "permissions": main_permissions,
        #         "order": 1,
        #     }]

        # labels = self._client.get("/projects/{}/labels".format(project_id))
        # tags_colors = []
        # for label in labels:
        #     name = label['name'].lower()
        #     tags_colors.append([name, None])
        #
        project = Project.objects.create(
            name=project['name'],
            description=project.get('description', ''),
            owner=self._user,
            # tags_colors=tags_colors,
            creation_template=project_template
        )

        UserStoryCustomAttribute.objects.create(
            name="Due date",
            description="Due date",
            type="date",
            order=1,
            project=project
        )

        TaskCustomAttribute.objects.create(
            name="Due date",
            description="Due date",
            type="date",
            order=1,
            project=project
        )

        IssueCustomAttribute.objects.create(
            name="Due date",
            description="Due date",
            type="date",
            order=1,
            project=project
        )

        EpicCustomAttribute.objects.create(
            name="Due date",
            description="Due date",
            type="date",
            order=1,
            project=project
        )

        greenhopper_fields = {}
        for custom_field in self._client.get("/field"):
            if custom_field['custom']:
                if custom_field['schema']['custom'] == "com.pyxis.greenhopper.jira:gh-sprint":
                    greenhopper_fields["sprint"] = custom_field['id']
                elif custom_field['schema']['custom'] == "com.pyxis.greenhopper.jira:gh-epic-link":
                    greenhopper_fields["link"] = custom_field['id']
                elif custom_field['schema']['custom'] == "com.pyxis.greenhopper.jira:gh-epic-status":
                    greenhopper_fields["status"] = custom_field['id']
                elif custom_field['schema']['custom'] == "com.pyxis.greenhopper.jira:gh-epic-label":
                    greenhopper_fields["label"] = custom_field['id']
                elif custom_field['schema']['custom'] == "com.pyxis.greenhopper.jira:gh-epic-color":
                    greenhopper_fields["color"] = custom_field['id']
                elif custom_field['schema']['custom'] == "com.pyxis.greenhopper.jira:gh-lexo-rank":
                    greenhopper_fields["rank"] = custom_field['id']
                elif (
                    custom_field['name'] == "Story Points" and
                    custom_field['schema']['custom'] == 'com.atlassian.jira.plugin.system.customfieldtypes:float'
                ):
                    greenhopper_fields["points"] = custom_field['id']
                else:
                    multiline_types = [
                        "com.atlassian.jira.plugin.system.customfieldtypes:textarea"
                    ]
                    date_types = [
                        "com.atlassian.jira.plugin.system.customfieldtypes:datepicker"
                        "com.atlassian.jira.plugin.system.customfieldtypes:datetime"
                    ]
                    if custom_field['schema']['custom'] in multiline_types:
                        field_type = "multiline"
                    elif custom_field['schema']['custom'] in date_types:
                        field_type = "date"
                    else:
                        field_type = "text"

                    custom_field_data = {
                        "name": custom_field['name'],
                        "description": custom_field['name'],
                        "type": field_type,
                        "order": 1,
                        "project": project
                    }

                    UserStoryCustomAttribute.objects.create(**custom_field_data)
                    TaskCustomAttribute.objects.create(**custom_field_data)
                    IssueCustomAttribute.objects.create(**custom_field_data)
                    EpicCustomAttribute.objects.create(**custom_field_data)

                import pprint; pprint.pprint(custom_field)
        project.greenhopper_fields = greenhopper_fields

        # for user in options.get('users_bindings', {}).values():
        #     if user != self._user:
        #         Membership.objects.get_or_create(
        #             user=user,
        #             project=project,
        #             role=project.get_roles().get(slug="main"),
        #             is_admin=False,
        #         )
        #
        # iterations = self._client.get("/projects/{}/iterations".format(project_id))
        # for iteration in iterations:
        #     milestone = Milestone.objects.create(
        #         name="Sprint {}".format(iteration['number']),
        #         slug="sprint-{}".format(iteration['number']),
        #         owner=self._user,
        #         project=project,
        #         estimated_start=iteration['start'][:10],
        #         estimated_finish=iteration['finish'][:10],
        #     )
        #     Milestone.objects.filter(id=milestone.id).update(
        #         created_date=iteration['start'],
        #         modified_date=iteration['start'],
        #     )
        return project

    def _import_user_stories_data(self, project_id, project, options):
        users_bindings = options.get('users_bindings', {})
        due_date_field = project.userstorycustomattributes.get(name="Due date")

        types = options.get('types_bindings', {}).get("us", [])
        for issue_type in types:
            counter = 0
            offset = 0
            while True:
                issues = self._client.get("/search", {
                    "jql": "project={} AND issuetype={}".format(project_id, issue_type['id']),
                    "startAt": offset
                })
                offset += issues['maxResults']

                for issue in issues['issues']:
                    assigned_to = users_bindings.get(issue['fields']['assignee']['key'] if issue['fields']['assignee'] else None, None)
                    owner = users_bindings.get(issue['fields']['creator']['key'] if issue['fields']['creator'] else None, self._user)

                    # sprint = issue['fields'][project.greenhopper_fields['sprint']]
                    # if sprint and len(sprint) == 1:
                    #     sprint[0][com.atlassian.greenhopper.service.sprint.Sprint@replace("/.*\[/", "").replace("\].*$"
                    # print(sprint)
                    Milestone.objects.create(
                        project=project,
                        name=sprint['name']

                    external_reference = None
                    if options.get('keep_external_reference', False):
                        external_reference = ["jira", issue['fields']['url']]

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
                    )

                    points_value = issue['fields'][project.greenhopper_fields['points']]
                    (points, _) = Points.objects.get_or_create(
                        project=project,
                        value=points_value,
                        defaults={
                            "name": str(points_value),
                            "order": points_value,
                        }
                    )
                    RolePoints.objects.filter(user_story=us, role__slug="main").update(points_id=points.id)

                    # for watcher in story['owner_ids'][1:]:
                    #     watcher_user = users_bindings.get(watcher, None)
                    #     if watcher_user:
                    #         us.add_watcher(watcher_user)

                    if issue['fields']['duedate']:
                        us.custom_attributes_values.attributes_values = {due_date_field.id: issue['fields']['duedate']}
                        us.custom_attributes_values.save()

                    UserStory.objects.filter(id=us.id).update(
                        ref=issue['key'].split("-")[1],
                        modified_date=issue['fields']['updated'],
                        created_date=issue['fields']['created']
                    )
                    take_snapshot(us, comment="", user=None, delete=False)
                    # self._import_attachments(us, card, options)
                    self._import_subtasks(project, us, issue, options)
                    # self._import_user_story_activity(project_id, us, story, options)
                    # self._import_comments(project_id, us, story, options)
                    counter += 1

                if len(issues['issues']) < issues['maxResults']:
                    break

    def _import_subtasks(self, project, us, issue, options):
        users_bindings = options.get('users_bindings', {})
        due_date_field = project.taskcustomattributes.get(name="Due date")

        if len(issue['fields']['subtasks']) == 0:
            return

        counter = 0
        offset = 0
        while True:
            issues = self._client.get("/search", {
                "jql": "parent={}".format(issue['key']),
                "startAt": offset
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
                )
                # for watcher in story['owner_ids'][1:]:
                #     watcher_user = users_bindings.get(watcher, None)
                #     if watcher_user:
                #         task.add_watcher(watcher_user)

                if issue['fields']['duedate']:
                    task.custom_attributes_values.attributes_values = {due_date_field.id: issue['fields']['duedate']}
                    task.custom_attributes_values.save()

                Task.objects.filter(id=task.id).update(
                    ref=issue['key'].split("-")[1],
                    modified_date=issue['fields']['updated'],
                    created_date=issue['fields']['created']
                )
                take_snapshot(task, comment="", user=None, delete=False)
                # self._import_attachments(us, card, options)
                for subtask in issue['fields']['subtasks']:
                    print("WARNING: Ignoring subtask {} because parent isn't a User Story".format(subtask['key']))
                # self._import_user_story_activity(project_id, us, story, options)
                # self._import_comments(project_id, us, story, options)
                counter += 1
            if len(issues['issues']) < issues['maxResults']:
                break

    def _import_issues_data(self, project_id, project, options):
        users_bindings = options.get('users_bindings', {})
        due_date_field = project.issuecustomattributes.get(name="Due date")

        types = options.get('types_bindings', {}).get("issue", [])
        for issue_type in types:
            counter = 0
            offset = 0
            while True:
                issues = self._client.get("/search", {
                    "jql": "project={} AND issuetype={}".format(project_id, issue_type['id']),
                    "startAt": offset
                })
                offset += issues['maxResults']

                for issue in issues['issues']:
                    assigned_to = users_bindings.get(issue['fields']['assignee']['key'] if issue['fields']['assignee'] else None, None)
                    owner = users_bindings.get(issue['fields']['creator']['key'] if issue['fields']['creator'] else None, self._user)

                    external_reference = None
                    if options.get('keep_external_reference', False):
                        external_reference = ["jira", issue['fields']['url']]

                    taiga_issue = Issue.objects.create(
                        project=project,
                        owner=owner,
                        assigned_to=assigned_to,
                        status=project.issue_statuses.get(name=issue['fields']['status']['name']),
                        subject=issue['fields']['summary'],
                        description=issue['fields']['description'] or '',
                        tags=issue['fields']['labels'],
                        external_reference=external_reference,
                    )
                    # for watcher in story['owner_ids'][1:]:
                    #     watcher_user = users_bindings.get(watcher, None)
                    #     if watcher_user:
                    #         taiga_issue.add_watcher(watcher_user)

                    if issue['fields']['duedate']:
                        taiga_issue.custom_attributes_values.attributes_values = {due_date_field.id: issue['fields']['duedate']}
                        taiga_issue.custom_attributes_values.save()

                    Issue.objects.filter(id=taiga_issue.id).update(
                        ref=issue['key'].split("-")[1],
                        modified_date=issue['fields']['updated'],
                        created_date=issue['fields']['created']
                    )
                    take_snapshot(taiga_issue, comment="", user=None, delete=False)
                    # self._import_attachments(us, card, options)
                    # self._import_tasks(project_id, us, story)
                    for subtask in issue['fields']['subtasks']:
                        print("WARNING: Ignoring subtask {} because parent isn't a User Story".format(subtask['key']))
                    # self._import_user_story_activity(project_id, us, story, options)
                    # self._import_comments(project_id, us, story, options)
                    counter += 1

                if len(issues['issues']) < issues['maxResults']:
                    break

    def _import_epics_data(self, project_id, project, options):
        users_bindings = options.get('users_bindings', {})
        due_date_field = project.epiccustomattributes.get(name="Due date")

        types = options.get('types_bindings', {}).get("epic", [])
        for issue_type in types:
            counter = 0
            offset = 0
            while True:
                issues = self._client.get("/search", {
                    "jql": "project={} AND issuetype={}".format(project_id, issue_type['id']),
                    "startAt": offset
                })
                offset += issues['maxResults']

                for issue in issues['issues']:
                    assigned_to = users_bindings.get(issue['fields']['assignee']['key'] if issue['fields']['assignee'] else None, None)
                    owner = users_bindings.get(issue['fields']['creator']['key'] if issue['fields']['creator'] else None, self._user)

                    external_reference = None
                    if options.get('keep_external_reference', False):
                        external_reference = ["jira", issue['fields']['url']]

                    epic_color = issue['fields'][project.greenhopper_fields['color']]
                    epic_color = {
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
                    }.get(epic_color, None)

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
                        color=epic_color,
                    )
                    # for watcher in story['owner_ids'][1:]:
                    #     watcher_user = users_bindings.get(watcher, None)
                    #     if watcher_user:
                    #         epic.add_watcher(watcher_user)

                    if issue['fields']['duedate']:
                        epic.custom_attributes_values.attributes_values = {due_date_field.id: issue['fields']['duedate']}
                        epic.custom_attributes_values.save()

                    Epic.objects.filter(id=epic.id).update(
                        ref=issue['key'].split("-")[1],
                        modified_date=issue['fields']['updated'],
                        created_date=issue['fields']['created']
                    )
                    take_snapshot(epic, comment="", user=None, delete=False)
                    # self._import_attachments(us, card, options)
                    # self._import_tasks(project_id, us, story)
                    for subtask in issue['fields']['subtasks']:
                        print("WARNING: Ignoring subtask {} because parent isn't a User Story".format(subtask['key']))
                    # self._import_user_story_activity(project_id, us, story, options)
                    # self._import_comments(project_id, us, story, options)
                    counter += 1

                if len(issues['issues']) < issues['maxResults']:
                    break

    def _link_epics_with_user_stories(self, project_id, project, options):
        types = options.get('types_bindings', {}).get("us", [])
        for issue_type in types:
            offset = 0
            while True:
                issues = self._client.get("/search", {
                    "jql": "project={} AND issuetype={}".format(project_id, issue_type['id']),
                    "startAt": offset
                })
                offset += issues['maxResults']

                for issue in issues['issues']:
                    epic_key = issue['fields'][project.greenhopper_fields['link']]
                    if epic_key:
                        epic = project.epics.get(ref=int(epic_key.split("-")[1]))
                        us = project.user_stories.get(ref=int(issue['key'].split("-")[1]))
                        RelatedUserStory.objects.create(
                            user_story=us,
                            epic=epic,
                            order=1
                        )

                if len(issues['issues']) < issues['maxResults']:
                    break

    # def _import_attachments(self, us, card, options):
    #     users_bindings = options.get('users_bindings', {})
    #     for attachment in card['attachments']:
    #         if attachment['bytes'] is None:
    #             continue
    #         data = requests.get(attachment['url'])
    #         att = Attachment(
    #             owner=users_bindings.get(attachment['idMember'], self._user),
    #             project=us.project,
    #             content_type=ContentType.objects.get_for_model(UserStory),
    #             object_id=us.id,
    #             name=attachment['name'],
    #             size=attachment['bytes'],
    #             created_date=attachment['date'],
    #             is_deprecated=False,
    #         )
    #         att.attached_file.save(attachment['name'], ContentFile(data.content), save=True)
    #
    #         UserStory.objects.filter(id=us.id, created_date__gt=attachment['date']).update(
    #             created_date=attachment['date']
    #         )
    #
    def _import_user_story_activity(self, project_id, us, story, options):
        included_activity = [
            "comment_create_activity", "comment_delete_activity",
            "comment_update_activity", "iteration_update_activity",
            "label_create_activity", "label_delete_activity",
            "label_update_activity", "story_create_activity",
            "story_delete_activity", "story_move_activity",
            "story_update_activity"
        ]

        offset = 0
        while True:
            activities = self._client.get(
                "/projects/{}/stories/{}/activity".format(
                    project_id,
                    story['id'],
                ),
                {"envelope": "true", "limit": 300, "offset": offset}
            )
            offset += 300
            for activity in activities['data']:
                if activity['kind'] in included_activity:
                    self._import_activity(us, activity, options)
            if len(activities['data']) < 300:
                break

    def _import_activity(self, obj, activity, options):
        key = make_key_from_model_object(obj)
        typename = get_typename_for_model_class(UserStory)
        activity_data = self._transform_activity_data(obj, activity, options)
        if activity_data is None:
            return

        change_old = activity_data['change_old']
        change_new = activity_data['change_new']
        hist_type = activity_data['hist_type']
        comment = activity_data['comment']
        user = activity_data['user']

        diff = make_diff_from_dicts(change_old, change_new)
        fdiff = FrozenDiff(key, diff, {})

        entry = HistoryEntry.objects.create(
            user=user,
            project_id=obj.project.id,
            key=key,
            type=hist_type,
            snapshot=None,
            diff=fdiff.diff,
            values=make_diff_values(typename, fdiff),
            comment=comment,
            comment_html=mdrender(obj.project, comment),
            is_hidden=False,
            is_snapshot=False,
        )
        HistoryEntry.objects.filter(id=entry.id).update(created_at=activity['occurred_at'])
        return HistoryEntry.objects.get(id=entry.id)

    def _transform_activity_data(self, obj, activity, options):
        users_bindings = options.get('users_bindings', {})
        due_date_field = obj.project.userstorycustomattributes.first()

        user = {"pk": None, "name": activity.get('performed_by', {}).get('name', None)}
        taiga_user = users_bindings.get(activity.get('performed_by', {}).get('id', None), None)
        if taiga_user:
            user = {"pk": taiga_user.id, "name": taiga_user.get_full_name()}

        result = {
            "change_old": {},
            "change_new": {},
            "hist_type": HistoryType.change,
            "comment": "",
            "user": user
        }

            # "comment_create_activity", "comment_delete_activity",
            # "comment_update_activity", "iteration_update_activity",
            # "label_create_activity", "label_delete_activity",
            # "label_update_activity",
            # "story_delete_activity", "story_move_activity",
            # "story_update_activity"
        if activity['kind'] == "comment_create_activity":
            for change in activity['changes']:
                if change['change_type'] == "comment":
                    result['comment'] = str(change['new_values']['text'])
        elif activity['kind'] == "story_create_activity":
            UserStory.objects.filter(id=obj.id, created_date__gt=activity['occurred_at']).update(
                created_date=activity['occurred_at'],
                owner=users_bindings.get(activity["performed_by"]["id"], self._user)
            )
            result['hist_type'] = HistoryType.create
            return None
        elif activity['kind'] == "copyCommentCard":
            # UserStory.objects.filter(id=us.id, created_date__gt=activity['date']).update(
            #     created_date=activity['date'],
            #     owner=users_bindings.get(activity["idMemberCreator"], self._user)
            # )
            # result['hist_type'] = HistoryType.create
            return None
        elif activity['kind'] == "createCard":
            # UserStory.objects.filter(id=us.id, created_date__gt=activity['date']).update(
            #     created_date=activity['date'],
            #     owner=users_bindings.get(activity["idMemberCreator"], self._user)
            # )
            # result['hist_type'] = HistoryType.create
            return None
        elif activity['kind'] == "story_update_activity":
            for change in activity['changes']:
                if change['change_type'] != "update" or change['kind'] != "story":
                    continue

                if 'description' in change['new_values']:
                    result['change_old']["description"] = str(change['original_values']['description'])
                    result['change_new']["description"] = str(change['new_values']['description'])
                    result['change_old']["description_html"] = mdrender(obj.project, str(change['original_values']['description']))
                    result['change_new']["description_html"] = mdrender(obj.project, str(change['new_values']['description']))

                if 'estimate' in change['new_values']:
                    pass

                if 'name' in change['new_values']:
                    result['change_old']["subject"] = change['original_values']['name']
                    result['change_new']["subject"] = change['new_values']['name']

                if 'labels' in change['new_values']:
                    result['change_old']["tags"] = [l.lower() for l in change['original_values']['labels']]
                    result['change_new']["tags"] = [l.lower() for l in change['new_values']['labels']]

                if 'current_state' in change['new_values']:
                    result['change_old']["status"] = obj.project.us_statuses.get(slug=change['original_values']['current_state']).id
                    result['change_new']["status"] = obj.project.us_statuses.get(slug=change['new_values']['current_state']).id

                # if 'due' in activity['data']['old']:
                #     result['change_old']["custom_attributes"] = [{
                #         "name": "Due",
                #         "value": activity['data']['old']['due'],
                #         "id": due_date_field.id
                #     }]
                #     result['change_new']["custom_attributes"] = [{
                #         "name": "Due",
                #         "value": activity['data']['card']['due'],
                #         "id": due_date_field.id
                #     }]
                #
                # if result['change_old'] == {}:
                #     return None
        return result

    @classmethod
    def get_auth_url(cls, server, consumer_key, key_cert_data, verify=None):
        if verify is None:
            verify = server.startswith('https')

        # step 1: get request tokens
        oauth = OAuth1(
            consumer_key, signature_method=SIGNATURE_RSA, rsa_key=key_cert_data)
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
