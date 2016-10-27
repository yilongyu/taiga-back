from requests_oauthlib import OAuth1Session
from django.conf import settings
from github import Github
from itertools import chain

from taiga.projects.models import Project, ProjectTemplate, Membership
from taiga.projects.userstories.models import UserStory
from taiga.projects.issues.models import Issue
from taiga.projects.history.services import take_snapshot
from taiga.projects.history.models import HistoryEntry
from taiga.users.models import User, AuthData


class GithubImporter:
    def __init__(self, user, token, import_closed_data=False):
        self._import_closed_data = import_closed_data
        self._user = user
        self._client = Github(token)

    def list_projects(self):
        user = self._client.get_user("jespino")
        return [{"id": repo.id, "name": repo.name} for repo in user.get_repos()]

    def import_project(self, project_id, options={"template": "kanban", "type": "user_stories"}):
        repo = self._client.get_repo(project_id)
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

        for user in repo.get_collaborators():
            taiga_user = self._get_user(user)
            if taiga_user is None or taiga_user == self._user:
                continue

            Membership.objects.create(
                user=taiga_user,
                project=project,
                role=project.get_roles().get(slug="github"),
                is_admin=False,
                invited_by=self._user,
            )

        return project

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

    def import_user_stories(self, project, project_id):
        repo = self._client.get_repo(project_id)
        issues = chain(repo.get_issues(state="open"), repo.get_issues(state="closed"))

        for issue in issues:
            tags = []
            for label in issue.labels:
                tags.append(label.name.lower())

            us = UserStory.objects.create(
                project=project,
                owner=self._get_user(issue.user, self._user),
                assigned_to=self._get_user(issue.assignee),
                status=project.us_statuses.get(slug=issue.state),
                kanban_order=issue.number,
                sprint_order=issue.number,
                backlog_order=issue.number,
                subject=issue.title,
                description=issue.body or "",
                tags=tags
            )
            UserStory.objects.filter(id=us.id).update(
                ref=issue.number,
                modified_date=issue.updated_at,
                created_date=issue.created_at
            )

            take_snapshot(us, comment="", user=None, delete=False)
            self._import_comments(us, issue)

    def import_issues(self, project, project_id):
        repo = self._client.get_repo(project_id)
        issues = chain(repo.get_issues(state="open"), repo.get_issues(state="closed"))

        for issue in issues:
            tags = []
            for label in issue.labels:
                tags.append(label.name.lower())

            taiga_issue = Issue.objects.create(
                project=project,
                owner=self._get_user(issue.user, self._user),
                assigned_to=self._get_user(issue.assignee),
                status=project.issue_statuses.get(slug=issue.state),
                subject=issue.title,
                description=issue.body or "",
                tags=tags
            )
            Issue.objects.filter(id=taiga_issue.id).update(
                ref=issue.number,
                modified_date=issue.updated_at,
                created_date=issue.created_at
            )

            take_snapshot(taiga_issue, comment="", user=None, delete=False)
            self._import_comments(taiga_issue, issue)

    def _import_comments(self, obj, issue):
        for comment in issue.get_comments():
            snapshot = take_snapshot(
                obj,
                comment=comment.body,
                user=self._get_user(comment.user, User(full_name=comment.user.name)),
                delete=False
            )
            HistoryEntry.objects.filter(id=snapshot.id).update(created_at=comment.created_at)

    @classmethod
    def get_auth_url(cls):
        request_token_url = 'https://trello.com/1/OAuthGetRequestToken'
        authorize_url = 'https://trello.com/1/OAuthAuthorizeToken'
        expiration = "never"
        scope = "read,write,account"
        trello_key = settings.TRELLO_API_KEY
        trello_secret = settings.TRELLO_SECRET_KEY
        name = "Taiga"

        session = OAuth1Session(client_key=trello_key, client_secret=trello_secret)
        response = session.fetch_request_token(request_token_url)
        oauth_token, oauth_token_secret = response.get('oauth_token'), response.get('oauth_token_secret')

        return (
            oauth_token,
            oauth_token_secret,
            "{authorize_url}?oauth_token={oauth_token}&scope={scope}&expiration={expiration}&name={name}".format(
                authorize_url=authorize_url,
                oauth_token=oauth_token,
                expiration=expiration,
                scope=scope,
                name=name,
            )
        )

    @classmethod
    def get_access_token(cls, oauth_token, oauth_token_secret, oauth_verifier):
        api_key = settings.TRELLO_API_KEY
        api_secret = settings.TRELLO_SECRET_KEY
        access_token_url = 'https://trello.com/1/OAuthGetAccessToken'
        session = OAuth1Session(client_key=api_key, client_secret=api_secret,
                                resource_owner_key=oauth_token, resource_owner_secret=oauth_token_secret,
                                verifier=oauth_verifier)
        access_token = session.fetch_access_token(access_token_url)
        return access_token
