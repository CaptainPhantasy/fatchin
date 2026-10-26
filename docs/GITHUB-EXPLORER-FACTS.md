# GitHub views: what works, what needs a correction

Checked against primary documentation on **2026-10-25**. This reviews the article supplied by the owner, “GitHub Secrets: Everything Hiding Behind One URL.” It describes current documented behavior, not a promise that another provider will stay available or keep its terms.

**The shortcuts are useful. The blanket claim “any public repository, free, no install” is too broad.** Some services need indexing, impose limits, or offer paid features. An AI explanation needs checking against the source. FatChin does not start GitHub Actions, Codespaces, a paid subscription, or another provider's model when you open its explorer.

| Article claim | Finding and correction | FatChin action |
| --- | --- | --- |
| Replace `github.com` with `gitdiagram.com` | Supported. The diagram samples the file tree, README and some source files. It cannot promise every file, a complete call graph, perfect accuracy or two-second generation. The provider documents rate limits and paid videos. [GitDiagram privacy](https://gitdiagram.com/privacy) | Architecture diagram |
| Replace it with `gitingest.com` | Supported. Produces a summary, directory tree and included text, with copy/download controls. Filters matter: the website currently starts with files under 50 kB. It is a snapshot. [GitIngest website](https://gitingest.com/), [maintainer repository](https://github.com/coderamp-labs/gitingest) | Text for an AI |
| Press `.` for `github.dev` | Supported. Free browser editing, search and source control; GitHub sign-in required. No terminal or code runtime; extensions must work in a browser. Uncommitted work lives in browser storage. [GitHub editor documentation](https://docs.github.com/en/codespaces/the-githubdev-web-based-editor) | Edit in your browser |
| `github1s.com` is the same place | Incorrect. GitHub1s is a separate service with a similar editor layout. Remote repositories are read-only; access/search depend on upstream limits. [GitHub1s maintainers](https://github.com/conwnet/github1s) | Read in a familiar editor |
| Replace it with `deepwiki.com` | Supported for public repository documentation and questions. A missing project can need submission and indexing. AI coverage and freshness vary; advanced Devin options have separate terms. [DeepWiki documentation](https://docs.devin.ai/work-with-devin/deepwiki) | Project explanation |
| Replace it with `gitmcp.io` | Supported for hosted documentation/code query tools through MCP, a connection format for AI tools. It does not create a writable clone. Client configuration differs; the documented Claude Desktop bridge uses Node/npx. [GitMCP maintainers](https://github.com/idosal/git-mcp) | Connect an AI reader |
| Add `?plain=1` to Markdown | Supported for Markdown source inside GitHub's interface, including line links. Use Raw for the file itself. [Markdown source links](https://docs.github.com/en/get-started/writing-on-github/working-with-advanced-formatting/creating-a-permanent-link-to-a-code-snippet), [Raw files](https://docs.github.com/en/repositories/working-with-files/using-files/viewing-and-understanding-files) | Markdown source / Raw file |
| Add `.patch` or `.diff` to a commit URL | Works for accessible commits; private repositories still require access. Merge patches can expand to a series of commits. Large/binary changes have text limits. Reading a patch does not apply it. [GitHub commit formats](https://docs.github.com/en/rest/commits/commits#get-a-commit); public URL probes below | Commit patch / Commit differences |
| `/compare/v1...v2` compares two versions | Supported, with an important distinction: three dots compare the common ancestor to the second version. Two dots compare the two endpoints directly. [GitHub branch comparison](https://docs.github.com/en/pull-requests/reference/branches#comparing-branches-in-pull-requests) | Compare versions |
| `/archive/refs/heads/main.zip` downloads the whole repository | Downloads a source snapshot if `main` exists. The default branch can have another name. ZIPs contain no Git history or submodule contents. LFS inclusion depends on settings and can consume the repository owner's LFS allowance. [Source archives](https://docs.github.com/en/repositories/working-with-files/using-files/downloading-source-code-archives), [LFS archive settings](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/managing-repository-settings/managing-git-lfs-objects-in-archives-of-your-repository) | Download this version, pinned to the selected commit |
| Add `.png` to a profile URL | Supported as a GitHub profile-picture shortcut; it can redirect to GitHub's avatar host. It is not proof that a project is trustworthy. [GitHub public profile data](https://docs.github.com/en/rest/users/users#get-a-user); public URL probe below | Project owner's picture |

Git archives contain the selected tree. A submodule records a reference to a separate project, not that project's tracked file tree; downloading the parent snapshot does not fetch that separate repository. See [Git's archive behavior](https://git-scm.com/docs/git-archive) and [GitHub's submodule representation](https://docs.github.com/en/rest/repos/contents#get-repository-content).

The five advertised keys are documented: `t` finds files; `y` pins a file link to its commit; `b` opens line history; `l` jumps to a line; `?` shows shortcuts for the current page. They depend on the page and keyboard focus, and character shortcuts can be disabled in accessibility settings. A pinned link avoids following branch edits but still needs access and an available commit. See [GitHub shortcuts](https://docs.github.com/en/get-started/accessibility/keyboard-shortcuts) and [permanent file links](https://docs.github.com/en/repositories/working-with-files/using-files/getting-permanent-links-to-files).

## Use these views in FatChin

Choose **Explore & learn**, select a project and branch, then open a folder or file. History shows recorded changes. Compare needs both a starting version (Base) and ending version (Head). Read the short explanation before opening an external view.

FatChin reads through its existing GitHub connection. It resolves the selected branch/tag to an exact commit before loading evidence. Each receipt distinguishes the actual `gh api --method GET` request from a corresponding Git command you could use in a local clone. It never runs that suggested local command against your project.

Listings use one API page: at most 100 branches, 100 history/compare commits and 1,000 folder entries. File previews show at most 64 KiB of UTF-8 text; binary or unavailable content is identified. GitHub's contents response can carry up to 1 MB before preview trimming. Comparisons show at most 100 changed files and 4,096 characters of patch per file. A full page is conservatively marked as possibly incomplete. Open GitHub for more. [GitHub contents limits](https://docs.github.com/en/rest/repos/contents#get-repository-content), [commit API](https://docs.github.com/en/rest/commits/commits).

Third-party repository links and MCP configurations appear only after GitHub explicitly reports the project as public. Private or unknown projects remain available through FatChin and official GitHub views. FatChin never sends its GitHub/model credentials to those sites. Opening an external link shares the public project address with that provider; the site can use its own index or default branch rather than FatChin's selected commit. External views open separately; FatChin does not observe or control them.

## Copy the setup for your AI client

Choose a client in **Connect an AI reader**, then copy its JSON. Merge the new entry into existing settings instead of replacing other servers. FatChin supplies repository-specific objects using the provider's [documented client examples](https://github.com/idosal/git-mcp#step-2-connect-your-ai-assistant):

| Client | Configuration shape | Existing client location |
| --- | --- | --- |
| Cursor | `mcpServers` → `repo-docs` → `url` | `~/.cursor/mcp.json` |
| Windsurf | `mcpServers` → `repo-docs` → `serverUrl` | `~/.codeium/windsurf/mcp_config.json` |
| VS Code | `servers` → `repo-docs` → `type: "sse"` and `url` | `.vscode/mcp.json` |
| Claude Desktop | `mcpServers` → `repo-docs` → `command: "npx"`, `args: ["mcp-remote", URL]` | Settings → Developer → Edit Config |

FatChin copies settings only. It does not install Node, a bridge, an extension or an MCP server, and does not overwrite any client configuration. Client versions can add native connection options; check their current documentation when configuring an existing installation.

## Verification receipts

The focused explorer tests exercise private/unknown visibility, separate client formats, ref/path encoding, invalid input rejection, immutable evidence, binary/oversize previews, UTF-8 boundaries, empty projects/history, comparison limits and API failures using an injected GitHub client. They make no GitHub writes.

Public URL checks on 2026-10-25 resolved `octocat/Hello-World`'s actual default branch, `master`, to `7fd1a60b01f91b314f59955a4e4d4e80d8edf11d`. Four GitHub API GETs returned the README text `Hello World!` with its newline. Separate HTTP probes sent no authentication:

| Public URL | Result |
| --- | --- |
| [Selected commit patch](https://github.com/octocat/Hello-World/commit/7fd1a60b01f91b314f59955a4e4d4e80d8edf11d.patch) | HTTP 200, `text/plain`; merge patch starts with an included commit |
| [Selected commit diff](https://github.com/octocat/Hello-World/commit/7fd1a60b01f91b314f59955a4e4d4e80d8edf11d.diff) | HTTP 200, `text/plain`; begins with a README diff |
| [Octocat profile picture](https://github.com/octocat.png) | HTTP 200, `image/png`; PNG file signature |

These probes establish those examples, not universal provider availability. No private project was sent to an external explorer.
