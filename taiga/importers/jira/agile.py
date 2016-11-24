import datetime

from django.template.defaultfilters import slugify
from taiga.projects.references.models import recalc_reference_counter
from taiga.projects.models import Project, ProjectTemplate, Points
from taiga.projects.userstories.models import UserStory, RolePoints
from taiga.projects.tasks.models import Task
from taiga.projects.milestones.models import Milestone
from taiga.projects.epics.models import Epic, RelatedUserStory
from taiga.projects.history.services import take_snapshot
from taiga.timeline.rebuilder import rebuild_timeline
from taiga.timeline.models import Timeline
from .common import JiraImporterCommon


class JiraAgileImporter(JiraImporterCommon):
    def list_projects(self):
        return self._client.get_agile('/board')['values']

    def import_project(self, project_id, options=None):
        project = self._import_project_data(project_id, options)
        self._import_epics_data(project_id, project, options)
        self._import_user_stories_data(project_id, project, options)
        self._cleanup(project, options)
        Timeline.objects.filter(project=project).delete()
        rebuild_timeline(None, None, project.id)
        recalc_reference_counter(project)

    def _import_project_data(self, project_id, options):
        project = self._client.get_agile("/board/{}".format(project_id))
        project_config = self._client.get_agile("/board/{}/configuration".format(project_id))
        if project['type'] == "scrum":
            project_template = ProjectTemplate.objects.get(slug="scrum")
            options['type'] = "scrum"
        elif project['type'] == "kanban":
            project_template = ProjectTemplate.objects.get(slug="kanban")
            options['type'] = "kanban"

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

        self._create_custom_fields(project)

        # for user in options.get('users_bindings', {}).values():
        #     if user != self._user:
        #         Membership.objects.get_or_create(
        #             user=user,
        #             project=project,
        #             role=project.get_roles().get(slug="main"),
        #             is_admin=False,
        #         )
        #
        if project_template.slug == "scrum":
            for sprint in self._client.get_agile("/board/{}/sprint".format(project_id))['values']:
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
        project_conf = self._client.get_agile("/board/{}/configuration".format(project_id))
        if options['type'] == "scrum":
            estimation_field = project_conf['estimation']['field']['fieldId']

        counter = 0
        offset = 0
        while True:
            issues = self._client.get_agile("/board/{}/issue".format(project_id), {
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

                if options['type'] == "scrum":
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

                self._import_to_custom_fields(us, issue, options)

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

        if len(issue['fields']['subtasks']) == 0:
            return

        counter = 0
        offset = 0
        while True:
            issues = self._client.get_agile("/board/{}/issue".format(project_id), {
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

                self._import_to_custom_fields(task, issue, options)

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
            issues = self._client.get_agile("/board/{}/epic".format(project_id), {
                "startAt": offset,
            })
            offset += issues['maxResults']

            for epic in issues['values']:
                issue = self._client.get_agile("/issue/{}".format(epic['key']))
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

                self._import_to_custom_fields(epic, issue, options)

                epic.ref = issue['key'].split("-")[1]
                Epic.objects.filter(id=epic.id).update(
                    ref=epic.ref,
                    modified_date=issue['fields']['updated'],
                    created_date=issue['fields']['created']
                )

                take_snapshot(epic, comment="", user=None, delete=False)
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
