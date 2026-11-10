"""MCP server giving an AI assistant search access to a Vaultwarden vault.

Secret values are only ever written to files, after a human approves each request; they are
never returned to the model.
"""
