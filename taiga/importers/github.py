import requests
from urllib.parse import parse_qsl
from github import Github
from itertools import chain
from django.core.files.base import ContentFile

from taiga.projects.models import Project, ProjectTemplate, Membership
from taiga.projects.references.models import recalc_reference_counter
from taiga.projects.userstories.models import UserStory
from taiga.projects.issues.models import Issue
from taiga.projects.milestones.models import Milestone
from taiga.projects.history.services import take_snapshot
from taiga.projects.history.services import (make_diff_from_dicts,
                                             make_diff_values,
                                             make_key_from_model_object,
                                             get_typename_for_model_class,
                                             FrozenDiff)
from taiga.projects.history.models import HistoryEntry
from taiga.projects.history.choices import HistoryType
from taiga.users.models import User, AuthData
from taiga.mdrender.service import render as mdrender


class GithubImporter:
    def __init__(self, user, token, import_closed_data=False):
        self._import_closed_data = import_closed_data
        self._user = user
        self._client = Github(token)

    def list_projects(self):
        user = self._client.get_user("jespino")
        return [{"id": repo.id, "name": repo.name} for repo in user.get_repos()]

    def list_users(self, project_id):
        repo = self._client.get_repo(project_id)
        return [{"id": u.id, "username": u.login, "full_name": u.name, "detected_user": self._get_user(u)} for u in repo.get_collaborators()]

    def _get_user(self, user, default=None):
        if not user:
            return default

        try:
            return AuthData.objects.get(key="github", value=user.id).user
        except AuthData.DoesNotExist:
            pass

        try:
            return User.objects.get(email=user.email)
        except User.DoesNotExist:
            pass

        return default

    def import_project(self, project_id, options={"template": "kanban", "type": "user_stories"}):
        repo = self._client.get_repo(project_id)
        project = self._import_project_data(repo, options)
        if options.get('type', None) == "user_stories":
            self._import_user_stories_data(project, repo, options)
        elif options.get('type', None) == "issues":
            self._import_issues_data(project, repo, options)
        recalc_reference_counter(project)

    def _import_project_data(self, repo, options):
        users_bindings = options.get('users_bindings', {})
        project_template = ProjectTemplate.objects.get(slug=options['template'])

        if options['type'] == "user_stories":
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
        elif options['type'] == "issues":
            project_template.issue_statuses = []
            project_template.issue_statuses.append({
                "name": "Open",
                "slug": "open",
                "is_closed": False,
                "color": "#ff8a84",
                "order": 1,
            })
            project_template.issue_statuses.append({
                "name": "Closed",
                "slug": "closed",
                "is_closed": True,
                "color": "#669900",
                "order": 2,
            })
            project_template.default_options["issue_status"] = "Open"

        project_template.roles.append({
            "name": "Github",
            "slug": "github",
            "computable": False,
            "permissions": project_template.roles[0]['permissions'],
            "order": 70,
        })

        tags_colors = []
        for label in repo.get_labels():
            name = label.name.lower()
            color = "#{}".format(label.color)
            tags_colors.append([name, color])

        project = Project.objects.create(
            name=repo.full_name,
            description=repo.description,
            owner=self._user,
            tags_colors=tags_colors,
            creation_template=project_template
        )

        if repo.organization and repo.organization.avatar_url:
            data = requests.get(repo.organization.avatar_url)
            project.logo.save("logo.png", ContentFile(data.content), save=True)

        for user in repo.get_collaborators():
            taiga_user = users_bindings.get(user.id, None)
            if taiga_user is None or taiga_user == self._user:
                continue

            Membership.objects.create(
                user=taiga_user,
                project=project,
                role=project.get_roles().get(slug="github"),
                is_admin=False,
                invited_by=self._user,
            )

        for milestone in repo.get_milestones():
            taiga_milestone = Milestone.objects.create(
                name=milestone.title,
                owner=users_bindings.get(milestone.creator.id, self._user),
                project=project,
                estimated_start=milestone.created_at,
                estimated_finish=milestone.due_on,
            )
            Milestone.objects.filter(id=taiga_milestone.id).update(
                created_date=milestone.created_at,
                modified_date=milestone.updated_at,
            )
        return project

    def _import_user_stories_data(self, project, repo, options):
        users_bindings = options.get('users_bindings', {})
        issues = chain(repo.get_issues(state="open"), repo.get_issues(state="closed"))

        for issue in issues:
            tags = []
            for label in issue.labels:
                tags.append(label.name.lower())

            assigned_to = users_bindings.get(issue.assignee.id, None) if issue.assignee else None
            us = UserStory.objects.create(
                project=project,
                owner=users_bindings.get(issue.user.id, self._user),
                milestone=project.milestones.get(name=issue.milestone.title) if issue.milestone else None,
                assigned_to=assigned_to,
                status=project.us_statuses.get(slug=issue.state),
                kanban_order=issue.number,
                sprint_order=issue.number,
                backlog_order=issue.number,
                subject=issue.title,
                description=issue.body or "",
                tags=tags
            )

            assignees = issue.raw_data.get('assignees', [])
            if len(assignees) > 1:
                for assignee in assignees:
                    if assignee['id'] != issue.assignee.id:
                        assignee_user = users_bindings.get(assignee['id'], None)
                        if assignee_user is not None:
                            us.add_watcher(assignee_user)

            UserStory.objects.filter(id=us.id).update(
                ref=issue.number,
                modified_date=issue.updated_at,
                created_date=issue.created_at
            )

            take_snapshot(us, comment="", user=None, delete=False)
            self._import_comments(us, issue, options)
            self._import_history(us, issue, options)

    def _import_issues_data(self, project, repo, options):
        users_bindings = options.get('users_bindings', {})
        issues = chain(repo.get_issues(state="open"), repo.get_issues(state="closed"))

        for issue in issues:
            tags = []
            for label in issue.labels:
                tags.append(label.name.lower())

            assigned_to = users_bindings.get(issue.assignee.id, None) if issue.assignee else None
            taiga_issue = Issue.objects.create(
                project=project,
                owner=users_bindings.get(issue.user.id, self._user),
                assigned_to=assigned_to,
                status=project.issue_statuses.get(slug=issue.state),
                subject=issue.title,
                description=issue.body or "",
                tags=tags
            )

            assignees = issue.raw_data.get('assignees', [])
            if len(assignees) > 1:
                for assignee in assignees:
                    if assignee['id'] != issue.assignee.id:
                        assignee_user = users_bindings.get(assignee['id'], None)
                        if assignee_user is not None:
                            taiga_issue.add_watcher(assignee_user)

            Issue.objects.filter(id=taiga_issue.id).update(
                ref=issue.number,
                modified_date=issue.updated_at,
                created_date=issue.created_at
            )

            take_snapshot(taiga_issue, comment="", user=None, delete=False)
            self._import_comments(taiga_issue, issue, options)
            self._import_history(taiga_issue, issue, options)

    def _import_comments(self, obj, issue, options):
        users_bindings = options.get('users_bindings', {})

        for comment in issue.get_comments():
            snapshot = take_snapshot(
                obj,
                comment=comment.body,
                user=users_bindings.get(comment.user.id, User(full_name=comment.user.name)),
                delete=False
            )
            HistoryEntry.objects.filter(id=snapshot.id).update(created_at=comment.created_at)

    def _import_history(self, obj, issue, options):
        key = make_key_from_model_object(obj)
        typename = get_typename_for_model_class(UserStory)

        cumulative_data = {
            "tags": set(),
            "assigned_to": None,
            "assigned_to_github_id": None,
            "assigned_to_name": None,
            "milestone": None,
        }
        for event in issue.get_events():
            event_data = self._import_event(obj, event, options, cumulative_data)
            if event_data is None:
                continue

            change_old = event_data['change_old']
            change_new = event_data['change_new']
            hist_type = event_data['hist_type']
            comment = event_data['comment']
            user = event_data['user']

            diff = make_diff_from_dicts(change_old, change_new)
            fdiff = FrozenDiff(key, diff, {})
            values = make_diff_values(typename, fdiff)
            values.update(event_data['update_values'])
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
            HistoryEntry.objects.filter(id=entry.id).update(created_at=event.created_at)

    def _import_event(self, obj, event, options, cumulative_data):
        users_bindings = options.get('users_bindings', {})

        ignored_events = ["committed", "cross-referenced", "head_ref_deleted",
                          "head_ref_restored", "locked", "unlocked", "merged",
                          "referenced", "mentioned", "subscribed",
                          "unsubscribed"]

        if event.event in ignored_events:
            return None

        user = {"pk": None, "name": event.actor.name}
        taiga_user = users_bindings.get(event.actor.id, None) if event.actor else None
        if taiga_user:
            user = {"pk": taiga_user.id, "name": taiga_user.get_full_name()}

        result = {
            "change_old": {},
            "change_new": {},
            "hist_type": HistoryType.change,
            "comment": "",
            "user": user,
            "update_values": {},
        }

        if event.event == "renamed":
            result['change_old']["subject"] = event.raw_data['rename']['from']
            result['change_new']["subject"] = event.raw_data['rename']['to']
        elif event.event == "reopened":
            if isinstance(obj, Issue):
                result['change_old']["status"] = obj.project.issue_statuses.get(name='Closed').id
                result['change_new']["status"] = obj.project.issue_statuses.get(name='Open').id
            elif isinstance(obj, UserStory):
                result['change_old']["status"] = obj.project.us_statuses.get(name='Closed').id
                result['change_new']["status"] = obj.project.us_statuses.get(name='Open').id
        elif event.event == "closed":
            if isinstance(obj, Issue):
                result['change_old']["status"] = obj.project.issue_statuses.get(name='Open').id
                result['change_new']["status"] = obj.project.issue_statuses.get(name='Closed').id
            elif isinstance(obj, UserStory):
                result['change_old']["status"] = obj.project.us_statuses.get(name='Open').id
                result['change_new']["status"] = obj.project.us_statuses.get(name='Closed').id
        elif event.event == "assigned":
            AssignedEventHandler(result, cumulative_data, users_bindings).handle(event)
        elif event.event == "unassigned":
            UnassignedEventHandler(result, cumulative_data, users_bindings).handle(event)
        elif event.event == "demilestoned":
            if isinstance(obj, UserStory):
                try:
                    result['change_old']["milestone"] = obj.project.milestones.get(name=event.raw_data['milestone']['title']).id
                except Milestone.DoesNotExist:
                    result['change_old']["milestone"] = 0
                    result['update_values'] = {"milestone": {"0": event.raw_data['milestone']['title']}}
                result['change_new']["milestone"] = None
                cumulative_data['milestone'] = None
        elif event.event == "milestoned":
            if isinstance(obj, UserStory):
                result['update_values']["milestone"] = {}
                if cumulative_data['milestone'] is not None:
                    result['update_values']['milestone'][str(cumulative_data['milestone'])] = cumulative_data['milestone_name']
                result['change_old']["milestone"] = cumulative_data['milestone']
                try:
                    taiga_milestone = obj.project.milestones.get(name=event.raw_data['milestone']['title'])
                    cumulative_data["milestone"] = taiga_milestone.id
                    cumulative_data['milestone_name'] = taiga_milestone.name
                except Milestone.DoesNotExist:
                    if cumulative_data['milestone'] == 0:
                        cumulative_data['milestone'] = -1
                    else:
                        cumulative_data['milestone'] = 0
                    cumulative_data['milestone_name'] = event.raw_data['milestone']['title']
                result['change_new']["milestone"] = cumulative_data['milestone']
                result['update_values']['milestone'][str(cumulative_data['milestone'])] = cumulative_data['milestone_name']
        elif event.event == "labeled":
            result['change_old']["tags"] = list(cumulative_data['tags'])
            cumulative_data['tags'].add(event.raw_data['label']['name'].lower())
            result['change_new']["tags"] = list(cumulative_data['tags'])
        elif event.event == "unlabeled":
            result['change_old']["tags"] = list(cumulative_data['tags'])
            cumulative_data['tags'].remove(event.raw_data['label']['name'].lower())
            result['change_new']["tags"] = list(cumulative_data['tags'])

        return result

    @classmethod
    def get_auth_url(cls, client_id):
        return "https://github.com/login/oauth/authorize?client_id={}&scope=user,repo".format(client_id)

    @classmethod
    def get_access_token(cls, client_id, client_secret, code):
        result = requests.post("https://github.com/login/oauth/access_token", {
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
        })
        return dict(parse_qsl(result.content))[b'access_token'].decode('utf-8')


class AssignedEventHandler:
    def __init__(self, result, cumulative_data, users_bindings):
        self.result = result
        self.cumulative_data = cumulative_data
        self.users_bindings = users_bindings

    def handle(self, event):
        if self.cumulative_data['assigned_to_github_id'] is None:
            self.result['update_values']["users"] = {}
            self.generate_change_old(event)
            self.generate_update_values_from_cumulative_data(event)
            user = self.users_bindings.get(event.raw_data['assignee']['id'], None)
            self.generate_change_new(event, user)
            self.update_cumulative_data(event, user)
            self.generate_update_values_from_cumulative_data(event)

    def generate_change_old(self, event):
        self.result['change_old']["assigned_to"] = self.cumulative_data['assigned_to']

    def generate_update_values_from_cumulative_data(self, event):
        if self.cumulative_data['assigned_to_name'] is not None:
            self.result['update_values']["users"][str(self.cumulative_data['assigned_to'])] = self.cumulative_data['assigned_to_name']

    def generate_change_new(self, event, user):
        if user is None:
            self.result['change_new']["assigned_to"] = 0
        else:
            self.result['change_new']["assigned_to"] = user.id

    def update_cumulative_data(self, event, user):
        self.cumulative_data['assigned_to_github_id'] = event.raw_data['assignee']['id']
        if user is None:
            self.cumulative_data['assigned_to'] = 0
            self.cumulative_data['assigned_to_name'] = event.raw_data['assignee']['login']
        else:
            self.cumulative_data['assigned_to'] = user.id
            self.cumulative_data['assigned_to_name'] = user.get_full_name()


class UnassignedEventHandler:
    def __init__(self, result, cumulative_data, users_bindings):
        self.result = result
        self.cumulative_data = cumulative_data
        self.users_bindings = users_bindings

    def handle(self, event):
        if self.cumulative_data['assigned_to_github_id'] == event.raw_data['assignee']['id']:
            self.result['update_values']["users"] = {}

            self.generate_change_old(event)
            self.generate_update_values_from_cumulative_data(event)
            self.generate_change_new(event)
            self.update_cumulative_data(event)
            self.generate_update_values_from_cumulative_data(event)

    def generate_change_old(self, event):
        self.result['change_old']["assigned_to"] = self.cumulative_data['assigned_to']

    def generate_update_values_from_cumulative_data(self, event):
        if self.cumulative_data['assigned_to_name'] is not None:
            self.result['update_values']["users"][str(self.cumulative_data['assigned_to'])] = self.cumulative_data['assigned_to_name']

    def generate_change_new(self, event):
        self.result['change_new']["assigned_to"] = None

    def update_cumulative_data(self, event):
        self.cumulative_data['assigned_to_github_id'] = None
        self.cumulative_data['assigned_to'] = None
        self.cumulative_data['assigned_to_name'] = None
