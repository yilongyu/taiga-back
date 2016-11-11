# -*- coding: utf-8 -*-
# Copyright (C) 2014-2016 Andrey Antukh <niwi@niwi.nz>
# Copyright (C) 2014-2016 Jesús Espino <jespinog@gmail.com>
# Copyright (C) 2014-2016 David Barragán <bameda@dbarragan.com>
# Copyright (C) 2014-2016 Alejandro Alonso <alejandro.alonso@kaleidos.net>
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

from django.core.management.base import BaseCommand
from django.db.models import Q
from django.conf import settings

from taiga.importers.jira import JiraImporter
from taiga.users.models import User
from taiga.projects.services import projects as service

import unittest.mock
import timeit
import json
from jira.jirashell import oauth_dance


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument('--token', dest="token", type=str,
                            help='Auth token')
        parser.add_argument('--project-id', dest="project_id", type=str,
                            help='Project ID or full name (ex: taigaio/taiga-back)')
        parser.add_argument('--template', dest='template', default="scrum",
                            help='template to use: scrum or scrum (default scrum)')
        parser.add_argument('--ask-for-users', dest='ask_for_users', const=True,
                            action="store_const", default=False,
                            help='Import closed data')
        parser.add_argument('--closed-data', dest='closed_data', const=True,
                            action="store_const", default=False,
                            help='Import closed data')
        parser.add_argument('--keep-external-reference', dest='keep_external_reference', const=True,
                            action="store_const", default=False,
                            help='Store external reference of imported data')

    def handle(self, *args, **options):
        admin = User.objects.get(username="admin")
        server = "http://jira.projects.kaleidos.net"
        for project in admin.projects.all():
            service.orphan_project(project)

        if options.get('token', None):
            token = json.loads(options.get('token'))
        else:
            with open(settings.JIRA_CERT_FILE, 'r') as key_cert_file:
                key_cert_data = key_cert_file.read()

            (rtoken, rtoken_secret, url) = JiraImporter.get_auth_url(server, "tribe-consumer", key_cert_data, True)
            print(url)
            code = input("Go to the url and get back the code")
            token = JiraImporter.get_access_token(server, "tribe-consumer", key_cert_data, rtoken, rtoken_secret, True)

        importer = JiraImporter(admin, server, token)

        if options.get('project_id', None):
            project_id = options.get('project_id')
        else:
            print("Select the project to import:")
            for project in importer.list_projects():
                print("- {} ({}): {}".format(project['id'], project['key'], project['name']))
            project_id = input("Project id or key: ")

        users_bindings = {}
        if options.get('ask_for_users', None):
            print("Add the username or email for next jira users:")
            for user in importer.list_users(project_id):
                try:
                    users_bindings[user['id']] = User.objects.get(Q(email=user['person']['email']))
                    break
                except User.DoesNotExist:
                    pass

                while True:
                    username_or_email = input("{}: ".format(user['person']['name']))
                    if username_or_email == "":
                        break
                    try:
                        users_bindings[user['id']] = User.objects.get(Q(username=username_or_email) | Q(email=username_or_email))
                        break
                    except User.DoesNotExist:
                        print("ERROR: Invalid username or email")

        print("Bind jira issue types to (epic, us, task, issue)")
        types_bindings = {
            "epic": [],
            "us": [],
            "task": [],
            "issue": [],
        }

        for issue_type in importer.list_issue_types(project_id):
            while True:
                if issue_type['subtask']:
                    types_bindings['task'].append(issue_type)
                    break

                taiga_type = input("{}: ".format(issue_type['name']))
                if taiga_type not in ['epic', 'us', 'issue']:
                    print("use a valid taiga type (epic, us, issue)")
                    continue

                types_bindings[taiga_type].append(issue_type)
                break

        options = {
            "template": options.get('template'),
            "import_closed_data": options.get("closed_data", False),
            "users_bindings": users_bindings,
            "keep_external_reference": options.get('keep_external_reference'),
            "types_bindings": types_bindings,
        }
        importer.import_project(project_id, options)
