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
from django.conf import settings

from taiga.importers.github import GithubImporter
from taiga.users.models import User, AuthData
from taiga.projects.services import projects as service

import unittest.mock
import timeit


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument('--token', dest="token", type=str,
                            help='Auth token')
        parser.add_argument('--project_id', dest="project_id", type=str,
                            help='Project ID or full name (ex: taigaio/taiga-back)')
        parser.add_argument('--template', dest='template', default="kanban",
                            help='template to use: scrum or kanban (default kanban)')
        parser.add_argument('--type', dest='type', default="user_stories",
                            help='type of object to use: user_stories or issues (default user_stories)')

    def handle(self, *args, **options):
        admin = User.objects.get(username="admin")
        for project in admin.projects.all():
            service.orphan_project(project)

        if options.get('token', None):
            token = options.get('token')
        else:
            url = GithubImporter.get_auth_url(settings.GITHUB_API_CLIENT_ID)
            print("Go to here and come with your code (in the redirected url): {}".format(url))
            code = input("Code: ")
            access_data = GithubImporter.get_access_token(settings.GITHUB_API_CLIENT_ID, settings.GITHUB_API_CLIENT_SECRET, code)
            token = access_data

        importer = GithubImporter(admin, token)

        if options.get('project_id', None):
            project_id = options.get('project_id')
        else:
            print("Select the project to import:")
            for project in importer.list_projects():
                print("- {}: {}".format(project['id'], project['name']))
            project_id = input("Project id: ")

        project = importer.import_project(project_id, {"template": options.get('template'), "type": options.get('type')})
        if options.get('type') == "user_stories":
            importer.import_user_stories(project, project_id)
        elif options.get('type') == "issues":
            importer.import_issues(project, project_id)
