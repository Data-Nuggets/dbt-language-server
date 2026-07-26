const { workspace } = require("vscode");
const { LanguageClient, TransportKind } = require("vscode-languageclient/node");

/** @type {import("vscode-languageclient/node").LanguageClient | undefined} */
let client;

function activate() {
  const config = workspace.getConfiguration("dbtLs");
  const command = config.get("serverCommand", "dbt-ls");
  const args = config.get("serverArgs", []);

  // The server speaks LSP over stdio; VS Code spawns `command` and connects.
  const serverOptions = {
    command,
    args,
    transport: TransportKind.stdio,
  };

  const clientOptions = {
    documentSelector: [{ scheme: "file", language: "sql" }],
    outputChannelName: "dbt-ls",
    synchronize: {
      // Reload when project config / compiled artifacts change.
      fileEvents: workspace.createFileSystemWatcher(
        "**/{dbt_project.yml,profiles.yml,target/catalog.json}"
      ),
    },
  };

  client = new LanguageClient(
    "dbtLs",
    "dbt Language Server",
    serverOptions,
    clientOptions
  );

  // NB: do NOT registerCommand("dbt-ls.reload") here. The server advertises it
  // via executeCommandProvider, so the LanguageClient auto-registers it (and
  // dbt-ls.current_model) and proxies to the server. Registering it again makes
  // client.start() throw "command already exists" and abort initialization.
  client.start();
}

function deactivate() {
  return client ? client.stop() : undefined;
}

module.exports = { activate, deactivate };
