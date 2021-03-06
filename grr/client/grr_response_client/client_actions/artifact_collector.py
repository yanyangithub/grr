#!/usr/bin/env python
"""The client artifact collector."""


from grr_response_client import actions
from grr_response_client import vfs
from grr_response_client.client_actions import admin
from grr_response_client.client_actions import file_finder
from grr_response_client.client_actions import network
from grr_response_client.client_actions import operating_system
from grr_response_client.client_actions import standard
from grr_response_client.client_actions.linux import linux
from grr_response_core.lib import artifact_utils
from grr_response_core.lib import parser
from grr_response_core.lib.rdfvalues import artifacts as rdf_artifacts
from grr_response_core.lib.rdfvalues import client_action as rdf_client_action
from grr_response_core.lib.rdfvalues import client_fs as rdf_client_fs
from grr_response_core.lib.rdfvalues import file_finder as rdf_file_finder
from grr_response_core.lib.rdfvalues import paths as rdf_paths


def _NotImplemented(args):
  # TODO(user): Not implemented yet. This method can be deleted once the
  # missing source types are supported.
  del args  # Unused
  raise NotImplementedError()


class ArtifactCollector(actions.ActionPlugin):
  """The client side artifact collector implementation."""

  in_rdfvalue = rdf_artifacts.ClientArtifactCollectorArgs
  out_rdfvalues = [rdf_artifacts.ClientArtifactCollectorResult]

  def Run(self, args):
    result = rdf_artifacts.ClientArtifactCollectorResult()
    self.knowledge_base = args.knowledge_base
    self.ignore_interpolation_errors = args.ignore_interpolation_errors
    for artifact in args.artifacts:
      self.Progress()
      collected_artifact = self._CollectArtifact(
          artifact, apply_parsers=args.apply_parsers)
      result.collected_artifacts.append(collected_artifact)

    # TODO(user): Limit the number of bytes and send multiple responses.
    # e.g. grr_rekall.py RESPONSE_CHUNK_SIZE
    self.SendReply(result)

  def _CollectArtifact(self, artifact, apply_parsers):
    artifact_result = rdf_artifacts.CollectedArtifact(name=artifact.name)

    processors = []
    if apply_parsers:
      processors = parser.Parser.GetClassesByArtifact(artifact.name)

    for source_result_list in self._ProcessSources(artifact.sources,
                                                   processors):
      for response in source_result_list:
        action_result = rdf_artifacts.ClientActionResult()
        action_result.type = response.__class__.__name__
        action_result.value = response
        artifact_result.action_results.append(action_result)

    return artifact_result

  def _ProcessSources(self, sources, processors):
    for source in sources:
      for action, request in self._ParseSourceType(source):
        yield self._RunClientAction(action, request, processors)

  def _RunClientAction(self, action, request, processors):
    saved_responses = []
    for response in action.Start(request):

      if processors:
        for processor in processors:
          processor_obj = processor()
          if processor_obj.process_together:
            raise NotImplementedError()
          for res in ParseResponse(processor_obj, response,
                                   self.knowledge_base):
            saved_responses.append(res)
      else:
        saved_responses.append(response)
    return saved_responses

  def _ParseSourceType(self, args):
    type_name = rdf_artifacts.ArtifactSource.SourceType
    switch = {
        type_name.COMMAND: self._ProcessCommandSource,
        type_name.DIRECTORY: _NotImplemented,
        type_name.FILE: self._ProcessFileSource,
        type_name.GREP: _NotImplemented,
        type_name.REGISTRY_KEY: _NotImplemented,
        type_name.REGISTRY_VALUE: self._ProcessRegistryValueSource,
        type_name.WMI: self._ProcessWmiSource,
        type_name.ARTIFACT_FILES: self._ProcessArtifactFilesSource,
        type_name.GRR_CLIENT_ACTION: self._ProcessClientActionSource
    }
    source_type = args.base_source.type

    try:
      source_type_action = switch[source_type]
    except KeyError:
      raise ValueError("Incorrect source type: %s" % source_type)

    for res in source_type_action(args):
      yield res

  def _ProcessArtifactFilesSource(self, args):
    """Get artifact responses, extract paths and send corresponding files."""

    if args.path_type != rdf_paths.PathSpec.PathType.OS:
      raise ValueError("Only supported path type is OS.")

    # TODO(user): Check paths for GlobExpressions.
    # If it contains a * then FileFinder will interpret it as GlobExpression and
    # expand it. FileFinderArgs needs an option to treat paths literally.

    paths = []
    source = args.base_source
    pathspec_attribute = source.attributes.get("pathspec_attribute")

    for source_result_list in self._ProcessSources(
        args.artifact_sources, processors=[]):
      for response in source_result_list:
        path = _ExtractPath(response, pathspec_attribute)
        if path is not None:
          paths.append(path)

    file_finder_action = rdf_file_finder.FileFinderAction.Download()
    request = rdf_file_finder.FileFinderArgs(
        paths=paths, pathtype=args.path_type, action=file_finder_action)
    action = file_finder.FileFinderOS

    yield action, request

  def _ProcessFileSource(self, args):

    if args.path_type != rdf_paths.PathSpec.PathType.OS:
      raise ValueError("Only supported path type is OS.")

    file_finder_action = rdf_file_finder.FileFinderAction.Stat()
    request = rdf_file_finder.FileFinderArgs(
        paths=args.base_source.attributes["paths"],
        pathtype=args.path_type,
        action=file_finder_action)
    action = file_finder.FileFinderOS

    yield action, request

  def _ProcessWmiSource(self, args):
    # pylint: disable= g-import-not-at-top
    from grr_response_client.client_actions.windows import windows
    # pylint: enable=g-import-not-at-top
    action = windows.WmiQuery
    query = args.base_source.attributes["query"]
    queries = artifact_utils.InterpolateKbAttributes(
        query, self.knowledge_base, self.ignore_interpolation_errors)
    base_object = args.base_source.attributes.get("base_object")
    for query in queries:
      request = rdf_client_action.WMIRequest(
          query=query, base_object=base_object)
      yield action, request

  def _ProcessClientActionSource(self, args):
    # TODO(user): Add support for remaining client actions
    # EnumerateFilesystems, StatFS and OSXEnumerateRunningServices
    switch_action = {
        "GetHostname": (admin.GetHostname, {}),
        "ListProcesses": (standard.ListProcesses, {}),
        "ListNetworkConnections": (
            network.ListNetworkConnections,
            rdf_client_action.ListNetworkConnectionsArgs()),
        "EnumerateInterfaces": (operating_system.EnumerateInterfaces, {}),
        "EnumerateUsers": (linux.EnumerateUsers, {}),
        # "EnumerateFilesystems": (operating_system.EnumerateFilesystems, {}),
        # "StatFS": (standard.StatFS, {}),
        # "OSXEnumerateRunningServices": (osx.OSXEnumerateRunningServices, {}),
    }
    action_name = args.base_source.attributes["client_action"]

    try:
      yield switch_action[action_name]
    except KeyError:
      raise ValueError("Incorrect action type: %s" % action_name)

  def _ProcessCommandSource(self, args):
    action = standard.ExecuteCommand
    request = rdf_client_action.ExecuteRequest(
        cmd=args.base_source.attributes["cmd"],
        args=args.base_source.attributes["args"],
    )
    yield action, request

  def _ProcessRegistryValueSource(self, args):
    new_paths = set()
    has_glob = False
    for kvdict in args.base_source.attributes["key_value_pairs"]:
      if "*" in kvdict["key"] or rdf_paths.GROUPING_PATTERN.search(
          kvdict["key"]):
        has_glob = True
      if kvdict["value"]:
        path = "\\".join((kvdict["key"], kvdict["value"]))
      else:
        path = kvdict["key"]
      expanded_paths = artifact_utils.InterpolateKbAttributes(
          path,
          self.knowledge_base,
          ignore_errors=self.ignore_interpolation_errors)
      new_paths.update(expanded_paths)
    if has_glob:
      # TODO(user): If a path has a wildcard we need to glob the filesystem
      # for patterns to collect matching files. The corresponding flow is
      # filesystem.Glob.
      pass
    else:
      action = standard.GetFileStat
      for new_path in new_paths:
        pathspec = rdf_paths.PathSpec(
            path=new_path, pathtype=rdf_paths.PathSpec.PathType.REGISTRY)
        request = rdf_client_action.GetFileStatRequest(pathspec=pathspec)
        yield action, request


# TODO(user): Think about a different way to call the Parse method of each
# supported parser. If the method signature is declared in the parser subtype
# classes then isinstance has to be used. And if it was declared in Parser then
# every Parser would have to be changed.
def ParseResponse(processor_obj, response, knowledge_base):
  """Call the parser for the response and yield rdf values.

  Args:
    processor_obj: An instance of the parser.
    response: An rdf value response from a client action.
    knowledge_base: containing information about the client.
  Returns:
    An iterable of rdf value responses.
  Raises:
    ValueError: If the requested parser is not supported.
  """
  if processor_obj.process_together:
    parse_method = processor_obj.ParseMultiple
  else:
    parse_method = processor_obj.Parse

  if isinstance(processor_obj, parser.CommandParser):
    result_iterator = parse_method(
        cmd=response.request.cmd,
        args=response.request.args,
        stdout=response.stdout,
        stderr=response.stderr,
        return_val=response.exit_status,
        time_taken=response.time_used,
        knowledge_base=knowledge_base)
  elif isinstance(processor_obj, parser.WMIQueryParser):
    # At the moment no WMIQueryParser actually uses the passed arguments query
    # and knowledge_base.
    result_iterator = parse_method(None, response, None)
  elif isinstance(processor_obj, parser.FileParser):
    if processor_obj.process_together:
      raise NotImplementedError()
    else:
      file_obj = vfs.VFSOpen(response.pathspec)
      stat = rdf_client_fs.StatEntry(pathspec=response.pathspec)
      result_iterator = parse_method(stat, file_obj, None)
  elif isinstance(processor_obj,
                  (parser.RegistryParser, parser.RekallPluginParser,
                   parser.RegistryValueParser, parser.GenericResponseParser,
                   parser.GrepParser)):
    result_iterator = parse_method(response, knowledge_base)
  elif isinstance(processor_obj, parser.ArtifactFilesParser):
    raise NotImplementedError()
  else:
    raise ValueError("Unsupported parser: %s" % processor_obj)
  return result_iterator


def _ExtractPath(response, pathspec_attribute=None):
  """Returns the path from a client action response as a string.

  Args:
    response: A client action response.
    pathspec_attribute: Specifies the field which stores the pathspec.

  Returns:
    The path as a string or None if no path is found.

  """
  path_specification = response

  if pathspec_attribute is not None:
    if response.HasField(pathspec_attribute):
      path_specification = response.Get(pathspec_attribute)

  if path_specification.HasField("pathspec"):
    path_specification = path_specification.pathspec

  if path_specification.HasField("path"):
    path_specification = path_specification.path

  if isinstance(path_specification, unicode):
    return path_specification
  return None
