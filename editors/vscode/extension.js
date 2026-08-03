const fs = require("fs");
const path = require("path");
const { RelativePattern, Uri, commands, window, workspace } = require("vscode");
const {
  ExecuteCommandRequest,
  LanguageClient,
  TransportKind,
} = require("vscode-languageclient/node");

// The server resolves exactly one dbt project per process, from the rootUri it
// receives in `initialize`. VS Code, unlike Neovim's root_markers, would
// otherwise start a single client rooted at the workspace folder — so a
// workspace holding several dbt projects gets one arbitrary project's
// completions everywhere. Instead we mirror what Neovim does: one client per
// dbt_project.yml, started lazily when a SQL file belonging to it is opened.

/** @type {Map<string, import("vscode-languageclient/node").LanguageClient>} */
const clients = new Map();

// Directories whose dbt_project.yml belongs to an installed package or to build
// output rather than to the user's project. Mirrors IGNORED_DIRS in
// src/dbt_ls/source.py.
const IGNORED_DIRS = new Set([
  ".git",
  ".venv",
  "dbt_packages",
  "node_modules",
  "target",
  "venv",
]);

class DbtLanguageClient extends LanguageClient {
  registerBuiltinFeatures() {
    super.registerBuiltinFeatures();
    // ExecuteCommandFeature calls commands.registerCommand() unconditionally for
    // every command the server advertises, so the second client to start would
    // throw "command already exists" and abort its own initialization. Drop the
    // feature and register dbt-ls.* once in activate() instead, routing each
    // call to the client that owns the file in question.
    const method = ExecuteCommandRequest.method;
    this._features = this._features.filter(
      (feature) => feature.registrationType?.method !== method
    );
    this._dynamicFeatures.delete(method);
  }
}

/**
 * Nearest ancestor of `filePath` containing a dbt_project.yml, or undefined.
 *
 * @param {string} filePath
 * @returns {string | undefined}
 */
function findDbtRoot(filePath) {
  const folder = workspace.getWorkspaceFolder(Uri.file(filePath));
  const boundary = folder ? folder.uri.fsPath : path.parse(filePath).root;

  const relative = path.relative(boundary, path.dirname(filePath));
  if (relative.startsWith("..")) {
    return undefined;
  }

  // Descend from the workspace folder toward the file, keeping the last
  // dbt_project.yml seen, so the *nearest* project wins when they nest.
  let root;
  let directory = boundary;
  if (fs.existsSync(path.join(directory, "dbt_project.yml"))) {
    root = directory;
  }
  for (const segment of relative ? relative.split(path.sep) : []) {
    if (IGNORED_DIRS.has(segment)) {
      break;
    }
    directory = path.join(directory, segment);
    if (fs.existsSync(path.join(directory, "dbt_project.yml"))) {
      root = directory;
    }
  }
  return root;
}

/**
 * @param {string} root
 * @param {string} fsPath
 */
function isInside(root, fsPath) {
  const relative = path.relative(root, fsPath);
  return relative === "" || !relative.startsWith("..");
}

/**
 * The client serving `root`, started on first use.
 *
 * @param {string} root
 */
function clientFor(root) {
  const existing = clients.get(root);
  if (existing) {
    return existing;
  }

  const config = workspace.getConfiguration("dbtLs");
  const rootUri = Uri.file(root);
  const name = path.basename(root);

  // The server speaks LSP over stdio; VS Code spawns `command` and connects.
  const serverOptions = {
    command: config.get("serverCommand", "dbt-ls"),
    args: config.get("serverArgs", []),
    transport: TransportKind.stdio,
  };

  const clientOptions = {
    // Scoping the selector to this root is what stops one project's server from
    // also serving its sibling's files.
    documentSelector: [
      {
        scheme: "file",
        language: "sql",
        pattern: new RelativePattern(rootUri, "**/*.sql"),
      },
    ],
    // Sent as rootUri/workspaceFolders in `initialize`, which is what the server
    // resolves its dbt project from. Synthetic on purpose: VS Code's own
    // workspace folder is the shared parent, not the project.
    workspaceFolder: { uri: rootUri, name, index: 0 },
    outputChannelName: `dbt-ls (${name})`,
    synchronize: {
      // Reload when project config / compiled artifacts change.
      fileEvents: workspace.createFileSystemWatcher(
        new RelativePattern(
          rootUri,
          "**/{dbt_project.yml,profiles.yml,target/catalog.json}"
        )
      ),
    },
    middleware: {
      workspace: {
        // The server registers its run_results watcher with a plain `**/` glob,
        // which VS Code resolves against every workspace folder. Drop events
        // from other projects so a sibling dbt run doesn't reload this one.
        didChangeWatchedFile: (event, next) =>
          isInside(root, Uri.parse(event.uri).fsPath)
            ? next(event)
            : Promise.resolve(),
      },
    },
  };

  const client = new DbtLanguageClient(
    "dbtLs",
    "dbt Language Server",
    serverOptions,
    clientOptions
  );
  clients.set(root, client);
  client.start();
  return client;
}

/** @param {import("vscode").TextDocument} document */
function ensureClientFor(document) {
  if (document.languageId !== "sql" || document.uri.scheme !== "file") {
    return;
  }
  const root = findDbtRoot(document.uri.fsPath);
  if (root) {
    clientFor(root);
  }
}

/**
 * Forward a server command to the client owning `uri`, defaulting to the active
 * editor's file.
 *
 * @param {string} command
 * @param {unknown[]} args
 * @param {Uri | undefined} uri
 */
function executeOnOwningClient(command, args, uri) {
  const target = uri ?? window.activeTextEditor?.document.uri;
  const root = target && target.scheme === "file" && findDbtRoot(target.fsPath);
  if (!root) {
    window.showWarningMessage(`${command}: no dbt project found for this file.`);
    return undefined;
  }
  return clientFor(root).sendRequest(ExecuteCommandRequest.type, {
    command,
    arguments: args,
  });
}

/** @param {import("vscode").ExtensionContext} context */
function activate(context) {
  context.subscriptions.push(
    workspace.onDidOpenTextDocument(ensureClientFor),
    workspace.onDidChangeWorkspaceFolders((event) => {
      for (const folder of event.removed) {
        for (const [root, client] of clients) {
          if (isInside(folder.uri.fsPath, root)) {
            clients.delete(root);
            client.stop();
          }
        }
      }
    }),
    commands.registerCommand("dbt-ls.reload", () =>
      executeOnOwningClient("dbt-ls.reload", [], undefined)
    ),
    commands.registerCommand("dbt-ls.current_model", (modelUri) => {
      const uri = typeof modelUri === "string" ? Uri.parse(modelUri) : modelUri;
      return executeOnOwningClient(
        "dbt-ls.current_model",
        [uri ? uri.toString() : window.activeTextEditor?.document.uri.toString()],
        uri
      );
    })
  );

  // onDidOpenTextDocument does not fire for documents already open when the
  // extension activates — including the one that triggered activation.
  workspace.textDocuments.forEach(ensureClientFor);
}

function deactivate() {
  const stopping = Array.from(clients.values(), (client) => client.stop());
  clients.clear();
  return Promise.all(stopping);
}

module.exports = { activate, deactivate };
