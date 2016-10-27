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
from taiga.projects.history.services import (take_snapshot,
                                             make_diff_from_dicts,
                                             make_diff_values,
                                             make_key_from_model_object,
                                             get_typename_for_model_class,
                                             FrozenDiff)
from taiga.projects.history.models import HistoryEntry
from taiga.projects.history.choices import HistoryType
from taiga.projects.custom_attributes.models import UserStoryCustomAttribute
from taiga.mdrender.service import render as mdrender
from taiga.users.models import User

# TODO: Read the organization avatar to use as project icon


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
        return self._client.fetch_json("/board?fields=id,name")

    def import_project(self, project_id):
        self._board = self._client.fetch_json("/board/{}?{}".format(
            project_id,
            "&".join([
                "fields=name,desc",
                "cards=all",
                "card_fields=closed,labels,idList,desc,due,name,pos,dateLastActivity,idChecklists",
                "card_attachments=true",
                "labels=all",
                "labels_limit=1000",
                "lists=all",
                "list_fields=closed,name,pos",
                "members=none",
                "checklists=all",
                "checklist_fields=name",
                "organization=true",
                "organization_fields=logoHash",
            ])
        ))
        board = self._board
        labels = board['labels']
        statuses = board['lists']
        kanban = ProjectTemplate.objects.get(slug="kanban")
        kanban.us_statuses = []
        counter = 0
        for us_status in statuses:
            if counter == 0:
                kanban.default_options["us_status"] = us_status['name']

            counter += 1
            if us_status['name'] not in [s['name'] for s in kanban.us_statuses]:
                kanban.us_statuses.append({
                    "name": us_status['name'],
                    "slug": slugify(us_status['name']),
                    "is_closed": False,
                    "is_archived": True if us_status['closed'] else False,
                    "color": "#999999",
                    "wip_limit": None,
                    "order": us_status['pos'],
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
            name = label['name']
            if not name:
                name = label['color']
            name = name.lower()
            color = self._ensure_hex_color(label['color'])
            tags_colors.append([name, color])

        project = Project.objects.create(
            name=board['name'],
            description=board['desc'],
            owner=self._user,
            tags_colors=tags_colors,
            creation_template=kanban
        )

        if board.get('organization', None):
            trello_avatar_template = "https://trello-logos.s3.amazonaws.com/{}/170.png"
            project_logo_url = trello_avatar_template.format(board['organization']['logoHash'])
            data = requests.get(project_logo_url)
            project.logo.save("logo.png", ContentFile(data.content), save=True)

        UserStoryCustomAttribute.objects.create(
            name="Due",
            description="Due date",
            type="date",
            order=1,
            project=project
        )
        return project

    def import_user_stories(self, project, project_id):
        statuses = {s['id']: s for s in self._board['lists']}
        cards = self._board['cards']

        has_due_date = False
        due_date_field = project.userstorycustomattributes.first()

        for card in cards:
            if card['closed'] and not self._import_closed_data:
                continue
            if statuses[card['idList']]['closed'] and not self._import_closed_data:
                continue

            tags = []
            for tag in card['labels']:
                name = tag['name']
                if not name:
                    name = tag['color']
                name = name.lower()
                tags.append(name)

            us = UserStory.objects.create(
                project=project,
                owner=self._user,
                status=project.us_statuses.get(name=statuses[card['idList']]['name']),
                kanban_order=card['pos'],
                sprint_order=card['pos'],
                backlog_order=card['pos'],
                subject=card['name'],
                description=card['desc'],
                tags=tags
            )
            UserStory.objects.filter(id=us.id).update(
                modified_date=card['dateLastActivity'],
                created_date=card['dateLastActivity']
            )

            if card['due']:
                has_due_date = True
                us.custom_attributes_values.attributes_values = {due_date_field.id: card['due']}
                us.custom_attributes_values.save()
            self._import_attachments(us, card)
            self._import_tasks(us, card)
            self._import_actions(us, card, statuses)

        if not has_due_date:
            due_date_field.delete()

    def _import_tasks(self, us, card):
        checklists_by_id = {c['id']: c for c in self._board['checklists']}
        for checklist_id in card['idChecklists']:
            for item in checklists_by_id.get(checklist_id, {}).get('checkItems', []):
                Task.objects.create(
                    subject=item['name'],
                    status=us.project.task_statuses.get(slug=item['state']),
                    project=us.project,
                    user_story=us
                )

    def _import_attachments(self, us, card):
        for attachment in card['attachments']:
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

    def _import_actions(self, us, card, statuses):
        included_actions = [
            "addAttachmentToCard", "addMemberToCard", "commentCard",
            "convertToCardFromCheckItem", "copyCommentCard", "createCard",
            "deleteAttachmentFromCard", "deleteCard", "removeMemberFromCard",
            "updateCard",
        ]

        actions = self._client.fetch_json("/card/{}/actions?{}".format(
            card['id'],
            "&".join([
                "filter={}".format(",".join(included_actions)),
                "limit=1000",
                "memberCreator=true",
                "memberCreator_fields=fullName",
            ])
        ))

        due_date_field = us.project.userstorycustomattributes.first()

        key = make_key_from_model_object(us)
        typename = get_typename_for_model_class(UserStory)
        while actions:
            print(card['id'], ":", len(actions))
            for action in actions:
                change_old = {}
                change_new = {}
                hist_type = HistoryType.change
                comment = ""
                user = {"pk": None, "name": action.get('memberCreator', {}).get('fullName', None)}
                is_hidden = False

                if action['type'] == "addAttachmentToCard":
                    continue
                elif action['type'] == "addMemberToCard":
                    continue
                elif action['type'] == "commentCard":
                    comment = str(action['data']['text'])
                elif action['type'] == "convertToCardFromCheckItem":
                    UserStory.objects.filter(id=us.id, created_date__gt=action['date']).update(
                        created_date=action['date']
                    )
                    hist_type = HistoryType.create
                elif action['type'] == "copyCommentCard":
                    UserStory.objects.filter(id=us.id, created_date__gt=action['date']).update(
                        created_date=action['date']
                    )
                    hist_type = HistoryType.create
                elif action['type'] == "createCard":
                    UserStory.objects.filter(id=us.id, created_date__gt=action['date']).update(
                        created_date=action['date']
                    )
                    hist_type = HistoryType.create
                elif action['type'] == "deleteAttachmentFromCard":
                    continue
                elif action['type'] == "deleteCard":
                    continue
                elif action['type'] == "removeMemberFromCard":
                    continue
                elif action['type'] == "updateCard":
                    if 'desc' in action['data']['old']:
                        change_old["description"] = str(action['data']['old']['desc'])
                        change_new["description"] = str(action['data']['card']['desc'])
                        change_old["description_html"] = mdrender(us.project, str(action['data']['old']['desc']))
                        change_new["description_html"] = mdrender(us.project, str(action['data']['card']['desc']))
                    if 'idList' in action['data']['old']:
                        change_old["status"] = us.project.us_statuses.get(name=statuses[action['data']['old']['idList']]['name']).id
                        change_new["status"] = us.project.us_statuses.get(name=statuses[action['data']['card']['idList']]['name']).id
                    if 'name' in action['data']['old']:
                        change_old["subject"] = action['data']['old']['name']
                        change_new["subject"] = action['data']['card']['name']
                    if 'due' in action['data']['old']:
                        change_old["custom_attributes"] = [{"name": "Due", "value": action['data']['old']['due'], "id": due_date_field.id}]
                        change_new["custom_attributes"] = [{"name": "Due", "value": action['data']['card']['due'], "id": due_date_field.id}]

                    if change_old == {}:
                        continue

                diff = make_diff_from_dicts(change_old, change_new)
                fdiff = FrozenDiff(key, diff, {})

                entry = HistoryEntry.objects.create(
                    user=user,
                    project_id=us.project.id,
                    key=key,
                    type=hist_type,
                    snapshot=None,
                    diff=fdiff.diff,
                    values=make_diff_values(typename, fdiff),
                    comment=comment,
                    comment_html=mdrender(us.project, comment),
                    is_hidden=is_hidden,
                    is_snapshot=False,
                )
                HistoryEntry.objects.filter(id=entry.id).update(created_at=action['date'])

            actions = self._client.fetch_json("/card/{}/actions?{}".format(
                card['id'],
                "&".join([
                    "filter={}".format(",".join(included_actions)),
                    "limit=1000",
                    "since=lastView",
                    "before={}".format(action['date']),
                    "memberCreator=true",
                    "memberCreator_fields=fullName",
                ])
            ))

    @classmethod
    def get_auth_url(cls):
        request_token_url = 'https://trello.com/1/OAuthGetRequestToken'
        authorize_url = 'https://trello.com/1/OAuthAuthorizeToken'
        expiration = "1day"
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

    def cleanup(self, project):
        if not self._import_closed_data:
            project.us_statuses.filter(is_archived=True).delete()
