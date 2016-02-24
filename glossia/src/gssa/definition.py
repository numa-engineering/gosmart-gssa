# This file is part of the Go-Smart Simulation Architecture (GSSA).
# Go-Smart is an EU-FP7 project, funded by the European Commission.
#
# Copyright (C) 2013-  NUMA Engineering Ltd. (see AUTHORS file)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
import os
import shutil
import tarfile
import logging

logger = logging.getLogger(__name__)

# Replace with better integrated approach!
import asyncio
from .transferrer import transferrer_register
from zope.interface.verify import verifyObject
from .transferrer import ITransferrer
from . import family as families

from lxml import etree as ET


# Routines for working with a single specific simulation
class GoSmartSimulationDefinition:
    _guid = None
    _dir = None
    _remote_dir = ''
    _finalized = False
    _files = None
    _exit_status = None
    _model_builder = None
    _shadowing = False

    # Set the status to be recorded in the DB
    def set_exit_status(self, success, message=None):
        self._exit_status = (success, message)

    def get_exit_status(self):
        return self._exit_status

    # Once we have a status connection from the simulation feed messages back to
    # the server
    @asyncio.coroutine
    def _handle_percentage_connection(self, stream_reader, stream_writer):
        logger.debug('Got percentage connection')
        while True:
            line = yield from stream_reader.readline()

            # Once we are out of data and the stream has closed, our job is done
            if not line:
                break

            # This is a very simplistic parsing approach, separating the string
            # based on the first pipe character. The percentage is first, the
            # status message the remainder.
            line = line.decode('utf-8').strip().split('|', maxsplit=1)
            percentage, message = (None, line[0]) if len(line) == 1 else line

            try:
                percentage = float(percentage)
            except ValueError:
                percentage = None

            # Call the server's callback
            self._update_status_callback(percentage, message)

    # Start up the status server
    @asyncio.coroutine
    def init_percentage_socket_server(self):
        if self._shadowing:
            logger.debug('No percentages: shadowing')
            self._percentage_socket_server = None
            return

        # Create the socket for the simulation to reach
        working_directory = self.get_dir()
        self._percentage_socket_location = self._model_builder.get_percentage_socket_location(working_directory)
        logger.debug('Status socket for %s : %s' % (self._guid, self._percentage_socket_location))
        try:
            # Start the socket server
            self._percentage_socket_server = yield from asyncio.start_unix_server(
                self._handle_percentage_connection,
                self._percentage_socket_location
            )
        except Exception as e:
            logger.debug('Could not connect to socket: %s' % str(e))
            self._percentage_socket_server = None

    def __init__(self, guid, xml_string, tmpdir, translator, finalized=False, ignore_development=False, update_status_callback=None):
        self._guid = guid
        self._dir = tmpdir
        self._finalized = finalized
        self._files = {}
        self._translator = translator
        self._update_status_callback = update_status_callback
        self._ignore_development = ignore_development

        if not finalized:
            # Do first parse of the GSSA-XML
            try:
                self.create_xml_from_string(xml_string)
            except Exception as e:
                logger.error(e)

            # Create the input directory, ready for the STL surfaces
            input_dir = os.path.join(tmpdir, 'input')
            if not os.path.exists(input_dir):
                try:
                    os.mkdir(input_dir)
                except Exception:
                    logger.exception('Could not create input directory')

            # Write the GSSA-XML there for safekeeping
            with open(os.path.join(tmpdir, "original.xml"), "w") as f:
                f.write(xml_string)

            # Make a note of the client GUID, in case we need to track backwards
            with open(os.path.join(tmpdir, "guid"), "w") as f:
                f.write(guid)

    # This directory indicates where on the client's system we should be
    # pulling/pushing from/to
    def get_remote_dir(self):
        return self._remote_dir

    def set_remote_dir(self, remote_dir):
        self._remote_dir = remote_dir

    # Find the GUID
    def get_guid(self):
        return self._guid

    # Turn the XML into an ElementTree object
    def create_xml_from_string(self, xml):
        self._finalized = False

        try:
            self._xml = ET.fromstring(bytes(xml, 'utf-8'))
        except Exception as e:
            logger.exception('Could not create XML from input')
            raise e

        return True

    # Wraps the file transferrer
    def update_files(self, files):
        self._files.update(files)

    def get_files(self):
        return self._files

    # Do the heavy lifting of interpreting the GSSA-XML
    def finalize(self):
        logger.debug("Finalize - Translating Called")
        if self._xml is None:
            return False

        try:
            logger.debug("Instantiating transferrer")

            # Discover what kind of transferrer (e.g. via /tmp, via SFTP) we
            # have been asked to use and create it
            transferrer_node = self._xml.find('transferrer')
            cls = transferrer_node.get('class')
            self._transferrer = transferrer_register[cls]()
            verifyObject(ITransferrer, self._transferrer)
            # Configure the transferrer from this node
            self._transferrer.configure_from_xml(transferrer_node)

            logger.debug("Starting to Translate")
            # Run the translator, which understands the higher-level, generic
            # concepts of the GSSA-XML
            family, numerical_model_node, parameters, algorithms = \
                self._translator.translate(self._xml)

            if family is None or family not in families.register:
                raise RuntimeError("Unknown family of models : %s" % family)

            # If we must ignore DEVELOPMENT='true' runs, and if this is one, then do so
            if self._ignore_development and 'DEVELOPMENT' in parameters and parameters['DEVELOPMENT']:
                self._shadowing = True
                logger.warning("Shadowing mode ON for this definition")
            else:
                files_required = self._translator.get_files_required()

                # Set up the model, most of the rest of the work is done here
                self._model_builder = families.register[family](files_required)
                self._model_builder.load_definition(numerical_model_node, parameters=parameters, algorithms=algorithms)

                self._files.update(files_required)
                self._transferrer.connect()
                # Pull down the input/definition files
                self._transferrer.pull_files(self._files, self.get_dir(), self.get_remote_dir())
                self._transferrer.disconnect()
        except Exception:
            logger.exception('Could not finalize set-up')
            return False

        self._finalized = True
        return True

    def finalized(self):
        return self._finalized

    # Return working directory
    def get_dir(self):
        return self._dir

    # Clean out the working directory
    @asyncio.coroutine
    def clean(self):
        yield from self._model_builder.clean()

        shutil.rmtree(self._dir)

        return True

    # Create a results archive
    def gather_results(self):
        output_directory = os.path.join(self.get_dir(), 'output')
        output_final_directory = os.path.join(self.get_dir(), 'output.final')

        result_files = {
            'output': output_directory,
            'output.final': output_final_directory,
            'original.xml': os.path.join(self.get_dir(), 'original.xml'),
            'guid': os.path.join(self.get_dir(), 'guid'),
        }

        return self._gather_files('results_archive.tgz', result_files)

    # Create a diagnostic archive
    def gather_diagnostic(self):
        input_directory = os.path.join(self.get_dir(), 'input')
        input_final_directory = os.path.join(self.get_dir(), 'input.final')
        output_directory = os.path.join(self.get_dir(), 'output')
        log_directory = os.path.join(output_directory, 'logs')

        diagnostic_files = {
            'input': input_directory,
            'input.final': input_final_directory,
            'logs': log_directory,
            'original.xml': os.path.join(self.get_dir(), 'original.xml'),
            'guid': os.path.join(self.get_dir(), 'guid'),
        }

        return self._gather_files('diagnostic_archive.tgz', diagnostic_files)

    # Turn a list of files into an archive
    def _gather_files(self, archive_name, files):
        output_directory = os.path.join(self.get_dir(), 'output')
        missing_file = os.path.join(output_directory, 'missing.txt')

        os.makedirs(output_directory, exist_ok=True)

        logger.debug("Creating tarfile")

        archive = os.path.join(self.get_dir(), archive_name)

        with tarfile.open(archive, mode='w:gz') as definition_tar:
            with open(missing_file, 'w') as missing:
                for f, loc in files.items():
                    try:
                        definition_tar.add(loc, arcname='%s/%s' % (self._guid, f))
                    except Exception as e:
                        missing.write("Missing %s : %s\n" % (f, str(e)))
            definition_tar.add(missing_file, arcname='%s/diagnostic_missing.txt' % self._guid)

        logger.debug("Created tarfile")

        return archive

    # Send back the results
    def push_files(self, files):
        if self._shadowing:
            logger.warning("Not simulating: shadowing mode ON for this definition")
            return {}

        uploaded_files = {}

        for local, remote in files.items():
            path = os.path.join(self.get_dir(), local)
            if os.path.exists(path):
                uploaded_files[local] = remote
            else:
                logger.warning("Could not find %s for pushing" % path)

        self._transferrer.connect()
        self._transferrer.push_files(uploaded_files, self.get_dir(), self.get_remote_dir())
        self._transferrer.disconnect()

        return uploaded_files

    @asyncio.coroutine
    def simulate(self):
        if self._shadowing:
            logger.warning("Not simulating: shadowing mode ON for this definition")
            raise RuntimeError("Failing here to leave simulation for external server control")

        # Get our asyncio task from the model builder
        task = yield from self._model_builder.simulate(self.get_dir())

        output_directory = os.path.join(self.get_dir(), 'output')
        if not os.path.exists(output_directory):
            os.mkdir(output_directory)
        # Get files output by the model into the output directory (I think this
        # is primarily useful for the Docker modules, say, where they are not
        # already there)
        self._model_builder.retrieve_files(output_directory)

        return task

    @asyncio.coroutine
    def validation(self):
        if self._shadowing:
            logger.warning("Not validating: shadowing mode ON for this definition")
            return None

        # Run the validation step only
        task = yield from self._model_builder.validation(self.get_dir())

        return task
