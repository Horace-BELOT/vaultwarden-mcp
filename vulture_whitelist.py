"""Names vulture reports as unused but that are read by serialization or by frameworks."""

from vaultwarden_mcp import catalog, server

# Fields of the MCP tool results: read when FastMCP serializes them for the client.
catalog.ItemSummary.custom_field_names  # noqa: B018
server.StatusResult.cli_status  # noqa: B018
server.StatusResult.unlocked_here  # noqa: B018
server.SearchResult.total  # noqa: B018
server.WriteResult.note  # noqa: B018
server.LockResult.locked  # noqa: B018
server.CreateResult.also_written_to  # noqa: B018
server.UpdateResult.also_written_to  # noqa: B018
server.UpdateResult.previous_value_in_history  # noqa: B018
server.Collection.organization  # noqa: B018
