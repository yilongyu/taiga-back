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
from .common import JiraImporterCommon


class JiraNormalImporter(JiraImporterCommon):
    def list_projects(self):
        return self._client.get('/project')

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
