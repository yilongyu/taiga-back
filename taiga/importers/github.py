from requests_oauthlib import OAuth1Session
from django.conf import settings
from django.core.files.base import ContentFile
from django.contrib.contenttypes.models import ContentType
import requests
import webcolors
from github import Github
from itertools import chain

from django.template.defaultfilters import slugify
from taiga.projects.models import Project, ProjectTemplate
from taiga.projects.userstories.models import UserStory
from taiga.projects.tasks.models import Task
from taiga.projects.attachments.models import Attachment
from taiga.projects.history.services import take_snapshot
from taiga.projects.history.models import HistoryEntry
from taiga.projects.custom_attributes.models import UserStoryCustomAttribute
from taiga.users.models import User


class GithubImporter:
    def __init__(self, user, token, import_closed_data=False):
        self._import_closed_data = import_closed_data
        self._user = user
        self._client = Github(token)

    def list_projects(self):
        user = self._client.get_user("jespino")
        return [{"id": repo.id, "name": repo.name} for repo in user.get_repos()]

    def import_project(self, project_id):
        repo = self._client.get_repo(project_id)
        kanban = ProjectTemplate.objects.get(slug="kanban")

        kanban.us_statuses = []
        kanban.us_statuses.append({
            "name": "Open",
            "slug": "open",
            "is_closed": False,
            "is_archived": False,
            "color": "#ff8a84",
            "wip_limit": None,
            "order": 1,
        })
        kanban.us_statuses.append({
            "name": "Closed",
            "slug": "closed",
            "is_closed": True,
            "is_archived": False,
            "color": "#669900",
            "wip_limit": None,
            "order": 2,
        })
        kanban.default_options["us_status"] = "Open"

        tags_colors = []
        for label in repo.get_labels():
            name = label.name
            if not name:
                name = label.color
            name = name.lower()
            color = self._ensure_hex_color(label.color)
            tags_colors.append([name, color])

        project = Project.objects.create(
            name=repo.full_name,
            description=repo.description,
            owner=self._user,
            tags_colors=tags_colors,
            creation_template=kanban
        )
        return project

    def import_user_stories(self, project, project_id):
        repo = self._client.get_repo(project_id)
        issues = chain(repo.get_issues(state="open"), repo.get_issues(state="closed"))

        for issue in issues:
            tags = []
            for tag in issue.labels:
                tags.append(tag.name.lower())

            us = UserStory.objects.create(
                ref=issue.number,
                project=project,
                owner=self._user,
                status=project.us_statuses.get(slug=issue.state),
                kanban_order=issue.number,
                sprint_order=issue.number,
                backlog_order=issue.number,
                subject=issue.title,
                description=issue.body or "",
                tags=tags
            )
            UserStory.objects.filter(id=us.id).update(
                modified_date=issue.updated_at,
                created_date=issue.created_at
            )

            # self._import_attachments(us, card)
            take_snapshot(us, comment="", user=None, delete=False)
            self._import_comments(us, issue)

    def _import_attachments(self, us, card):
        for attachment in card.attachments:
            if attachment['bytes'] is None:
                continue
            data = requests.get(attachment['url'])
            att = Attachment(
                owner=self._user,
                project=us.project,
                content_type=ContentType.objects.get_for_model(UserStory),
                object_id=us.id,
                name=attachment['name'],
                size=attachment['bytes'],
                created_date=attachment['date'],
                is_deprecated=False,
            )
            att.attached_file.save(attachment['name'], ContentFile(data.content), save=True)

            UserStory.objects.filter(id=us.id, created_date__gt=attachment['date']).update(
                created_date=attachment['date']
            )

    def _import_comments(self, us, issue):
        for comment in issue.get_comments():
            snapshot = take_snapshot(
                us,
                comment=comment.body,
                user=User(full_name=comment.user.name),
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

    def _ensure_hex_color(self, color):
        if color is None:
            return None
        try:
            return webcolors.name_to_hex(color)
        except ValueError:
            return color
