from trello import TrelloClient
from requests_oauthlib import OAuth1Session
from django.conf import settings
from django.core.files.base import ContentFile
from django.contrib.contenttypes.models import ContentType
import requests
import webcolors

from django.template.defaultfilters import slugify
from taiga.projects.models import Project, ProjectTemplate
from taiga.projects.userstories.models import UserStory
from taiga.projects.tasks.models import Task
from taiga.projects.attachments.models import Attachment
from taiga.projects.history.services import take_snapshot
from taiga.projects.history.models import HistoryEntry
from taiga.projects.custom_attributes.models import UserStoryCustomAttribute, UserStoryCustomAttributesValues
from taiga.users.models import User


class TrelloImporter:
    def __init__(self, user, token, import_closed_data=False):
        self._import_closed_data = import_closed_data
        self._user = user
        self._client = TrelloClient(
            api_key=settings.TRELLO_API_KEY,
            api_secret=settings.TRELLO_SECRET_KEY,
            token=token,
        )

    def list_projects(self):
        boards = self._client.list_boards()
        return [{"id": board.id, "name": board.name} for board in boards]

    def import_project(self, project_id):
        board = self._client.get_board(project_id)
        labels = board.get_labels(limit=1000)
        statuses = board.all_lists()
        kanban = ProjectTemplate.objects.get(slug="kanban")
        kanban.us_statuses = []
        counter = 0
        for us_status in statuses:
            if us_status.closed and not self._import_closed_data:
                continue
            if counter == 0:
                kanban.default_options["us_status"] = us_status.name

            counter += 1
            kanban.us_statuses.append({
                "name": us_status.name,
                "slug": slugify(us_status.name),
                "is_closed": False,
                "is_archived": False,
                "color": "#999999",
                "wip_limit": None,
                "order": counter,
            })

        kanban.task_statuses = []
        kanban.task_statuses.append({
            "name": "Incomplete",
            "slug": "incomplete",
            "is_closed": False,
            "color": "#ff8a84",
            "order": 1,
        })
        kanban.task_statuses.append({
            "name": "Complete",
            "slug": "complete",
            "is_closed": True,
            "color": "#669900",
            "order": 2,
        })
        kanban.default_options["task_status"] = "Incomplete"
        tags_colors = []
        for label in labels:
            name = label.name
            if not name:
                name = label.color
            name = name.lower()
            color = self._ensure_hex_color(label.color)
            tags_colors.append([name, color])

        project = Project.objects.create(
            name=board.name,
            description=board.description,
            owner=self._user,
            tags_colors=tags_colors,
            creation_template=kanban
        )
        UserStoryCustomAttribute.objects.create(
            name="Due",
            description="Due date",
            type="date",
            order=1,
            project=project
        )
        return project

    def import_user_stories(self, project, project_id):
        board = self._client.get_board(project_id)
        statuses = {s.id: s for s in board.all_lists()}
        cards = board.all_cards()

        has_due_date = False
        due_date_field = project.userstorycustomattributes.first()

        for card in cards:
            if card.closed and not self._import_closed_data:
                continue
            if statuses[card.list_id].closed and not self._import_closed_data:
                continue
            card.fetch()

            tags = []
            for tag in card.labels:
                name = tag.name
                if not name:
                    name = tag.color
                name = name.lower()
                tags.append(name)

            us = UserStory.objects.create(
                project=project,
                owner=self._user,
                status=project.us_statuses.get(name=statuses[card.list_id].name),
                kanban_order=card.pos,
                sprint_order=card.pos,
                backlog_order=card.pos,
                subject=card.name,
                description=card.description,
                tags=tags
            )
            UserStory.objects.filter(id=us.id).update(
                modified_date=card.date_last_activity,
                created_date=card.date_last_activity
            )

            if card.due:
                has_due_date = True
                us.custom_attributes_values.attributes_values = {due_date_field.id: card.due}
                us.custom_attributes_values.save()
            self._import_attachments(us, card)
            self._import_tasks(us, card)
            take_snapshot(us, comment="", user=None, delete=False)
            self._import_comments(us, card)

        if not has_due_date:
            due_date_field.delete()

    def _import_tasks(self, us, card):
        for checklist in card.fetch_checklists():
            for item in checklist.items:
                Task.objects.create(
                    subject=item['name'],
                    status=us.project.task_statuses.get(slug=item['state']),
                    project=us.project,
                    user_story=us
                )

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

    def _import_comments(self, us, card):
        for comment in card.fetch_comments(limit=1000):
            snapshot = take_snapshot(
                us,
                comment=comment['data']['text'],
                user=User(full_name=comment.get('memberCreator', {}).get('fullName', None)),
                delete=False
            )
            HistoryEntry.objects.filter(id=snapshot.id).update(created_at=comment['date'])
            UserStory.objects.filter(id=us.id, created_date__gt=comment['date']).update(
                created_date=comment['date']
            )

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
