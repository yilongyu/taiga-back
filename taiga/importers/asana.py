import requests
import asana
from django.core.files.base import ContentFile
from django.contrib.contenttypes.models import ContentType

from taiga.projects.models import Project, ProjectTemplate, Membership
from taiga.projects.userstories.models import UserStory
from taiga.projects.tasks.models import Task
from taiga.projects.attachments.models import Attachment
from taiga.projects.history.services import take_snapshot
from taiga.projects.history.models import HistoryEntry
from taiga.users.models import User


class AsanaImporter:
    def __init__(self, user, token, import_closed_data=False):
        self._import_closed_data = import_closed_data
        self._user = user
        print(token)
        self._client = asana.Client.oauth(token=token)

    def list_projects(self):
        projects = []
        for ws in self._client.workspaces.find_all():
            for project in self._client.projects.find_all(workspace=ws['id']):
                projects.append({"id": project['id'], "name": "{}/{}".format(ws['name'], project['name'])})
        return projects

    def list_users(self, project_id):
        users = []
        for ws in self._client.workspaces.find_all():
            for user in self._client.users.find_by_workspace(ws['id'], fields=["id", "name", "email"]):
                users.append({
                    "id": user["id"],
                    "full_name": user['name'],
                    "detected_user": self._get_user(user)
                })
        return users

    def _get_user(self, user, default=None):
        if not user:
            return default

        try:
            return User.objects.get(email=user['email'])
        except User.DoesNotExist:
            pass

        return default

    def import_project(self, project_id, options={"keep_external_reference": False, "template": "kanban"}):
        project = self._client.projects.find_by_id(project_id)
        taiga_project = self._import_project_data(project, options)
        self._import_user_stories_data(taiga_project, project, options)

    def _import_project_data(self, project, options):
        users_bindings = options.get('users_bindings', {})
        project_template = ProjectTemplate.objects.get(slug=options['template'])

        project_template.us_statuses = []
        project_template.us_statuses.append({
            "name": "Open",
            "slug": "open",
            "is_closed": False,
            "is_archived": False,
            "color": "#ff8a84",
            "wip_limit": None,
            "order": 1,
        })
        project_template.us_statuses.append({
            "name": "Closed",
            "slug": "closed",
            "is_closed": True,
            "is_archived": False,
            "color": "#669900",
            "wip_limit": None,
            "order": 2,
        })
        project_template.default_options["us_status"] = "Open"

        project_template.task_statuses = []
        project_template.task_statuses.append({
            "name": "Open",
            "slug": "open",
            "is_closed": False,
            "color": "#ff8a84",
            "order": 1,
        })
        project_template.task_statuses.append({
            "name": "Closed",
            "slug": "closed",
            "is_closed": True,
            "color": "#669900",
            "order": 2,
        })
        project_template.default_options["task_status"] = "Open"

        project_template.roles.append({
            "name": "Asana",
            "slug": "asana",
            "computable": False,
            "permissions": project_template.roles[0]['permissions'],
            "order": 70,
        })

        tags_colors = []
        for tag in self._client.tags.find_by_workspace(project['workspace']['id'], fields=["name", "color"]):
            name = tag['name'].lower()
            color = tag['color']
            tags_colors.append([name, color])

        taiga_project = Project.objects.create(
            name=project['name'],
            description=project['notes'],
            owner=self._user,
            tags_colors=tags_colors,
            creation_template=project_template
        )

        for user in self._client.users.find_by_workspace(project['workspace']['id']):
            taiga_user = users_bindings.get(user['id'], None)
            if taiga_user is None or taiga_user == self._user:
                continue

            Membership.objects.create(
                user=taiga_user,
                project=taiga_project,
                role=taiga_project.get_roles().get(slug="asana"),
                is_admin=False,
                invited_by=self._user,
            )

        return taiga_project

    def _import_user_stories_data(self, taiga_project, project, options):
        users_bindings = options.get('users_bindings', {})
        tasks = self._client.tasks.find_by_project(
            project['id'],
            fields=["parent", "tags", "name", "notes", "tags.name",
                    "completed", "followers", "modified_at", "created_at",
                    "project"]
        )

        for task in tasks:
            if task['parent']:
                continue

            tags = []
            for tag in task['tags']:
                tags.append(tag['name'].lower())

            assigned_to = users_bindings.get(task.get('assignee', {}).get('id', None)) or None

            external_reference = None
            if options.get('keep_external_reference', False):
                external_url = "https://app.asana.com/0/{}/{}".format(
                    project['id'],
                    task['id'],
                )
                external_reference = ["asana", external_url]

            us = UserStory.objects.create(
                project=taiga_project,
                owner=self._user,
                assigned_to=assigned_to,
                status=taiga_project.us_statuses.get(slug="closed" if task['completed'] else "open"),
                kanban_order=task['id'],
                sprint_order=task['id'],
                backlog_order=task['id'],
                subject=task['name'],
                description=task.get('notes', ""),
                tags=tags,
                external_reference=external_reference
            )

            for follower in task['followers']:
                follower_user = users_bindings.get(follower['id'], None)
                if follower_user is not None:
                    us.add_watcher(follower_user)

            UserStory.objects.filter(id=us.id).update(
                modified_date=task['modified_at'],
                created_date=task['created_at']
            )

            subtasks = self._client.tasks.subtasks(
                task['id'],
                fields=["parent", "tags", "name", "notes", "tags.name",
                        "completed", "followers", "modified_at", "created_at"]
            )
            for subtask in subtasks:
                self._import_task_data(taiga_project, us, project, subtask, options)

            take_snapshot(us, comment="", user=None, delete=False)
            self._import_history(us, task, options)
            self._import_attachments(us, task, options)

    def _import_task_data(self, taiga_project, us, assana_project, task, options):
        users_bindings = options.get('users_bindings', {})
        tags = []
        for tag in task['tags']:
            tags.append(tag['name'].lower())

        assigned_to = users_bindings.get(task.get('assignee', {}).get('id', None)) or None

        external_reference = None
        if options.get('keep_external_reference', False):
            external_url = "https://app.asana.com/0/{}/{}".format(
                assana_project['id'],
                task['id'],
            )
            external_reference = ["asana", external_url]

        taiga_task = Task.objects.create(
            project=taiga_project,
            user_story=us,
            owner=self._user,
            assigned_to=assigned_to,
            status=taiga_project.task_statuses.get(slug="closed" if task['completed'] else "open"),
            us_order=task['id'],
            taskboard_order=task['id'],
            subject=task['name'],
            description=task.get('notes', ""),
            tags=tags,
            external_reference=external_reference
        )

        for follower in task['followers']:
            follower_user = users_bindings.get(follower['id'], None)
            if follower_user is not None:
                taiga_task.add_watcher(follower_user)

        Task.objects.filter(id=taiga_task.id).update(
            modified_date=task['modified_at'],
            created_date=task['created_at']
        )

        take_snapshot(taiga_task, comment="", user=None, delete=False)
        self._import_history(taiga_task, task, options)
        self._import_attachments(taiga_task, task, options)

    def _import_history(self, obj, task, options):
        users_bindings = options.get('users_bindings', {})
        stories = self._client.stories.find_by_task(task['id'])
        for story in stories:
            if story['type'] == "comment":
                snapshot = take_snapshot(
                    obj,
                    comment=story['text'],
                    user=users_bindings.get(story['created_by']['id'], User(full_name=story['created_by']['name'])),
                    delete=False
                )
                HistoryEntry.objects.filter(id=snapshot.id).update(created_at=story['created_at'])

    def _import_attachments(self, obj, task, options):
        attachments = self._client.attachments.find_by_task(
            task['id'],
            fields=['name', 'download_url', 'created_at']
        )
        for attachment in attachments:
            data = requests.get(attachment['download_url'])
            att = Attachment(
                owner=self._user,
                project=obj.project,
                content_type=ContentType.objects.get_for_model(obj),
                object_id=obj.id,
                name=attachment['name'],
                size=len(data.content),
                created_date=attachment['created_at'],
                is_deprecated=False,
            )
            att.attached_file.save(attachment['name'], ContentFile(data.content), save=True)

    @classmethod
    def get_auth_url(cls, client_id, client_secret, callback_url=None):
        client = asana.Client.oauth(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=callback_url
        )
        (url, state) = client.session.authorization_url()
        return url

    @classmethod
    def get_access_token(cls, code, client_id, client_secret, callback_url=None):
        client = asana.Client.oauth(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=callback_url
        )
        return client.session.fetch_token(code=code)
