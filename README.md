# mcp-pubmed

A **Model Context Protocol (MCP)** server that gives Claude (or any MCP-compatible AI) direct access to PubMed and PubMed Central via the free NCBI E-utilities API.

No subscription required. No third-party service. Pure NCBI.

---

## Features

| Tool | Description |
|------|-------------|
| `search_pubmed` | Search articles by keyword, with filters for date range, article type, and sort order. Supports full PubMed query syntax. |
| `get_article` | Retrieve complete metadata for a single article by PMID (abstract, authors, MeSH terms, keywords, DOI, PMC link). |
| `get_full_text` | Download the full text from PubMed Central when the article is open-access. |
| `get_related_articles` | Find articles related to a given PMID using NCBI's similarity algorithm. |
| `search_by_author` | List all articles published by a specific author, sorted by most recent. |

---

## Requirements

- Python **3.11** or higher
- pip

---

## Installation

### 1 — Clone the repository

```bash
git clone https://github.com/benoitleq/mcp-pubmed.git
cd mcp-pubmed
```

### 2 — Create a virtual environment

```bash
python -m venv .venv
```

Activate it:

- **Windows (PowerShell)**  : `.venv\Scripts\Activate.ps1`
- **Windows (CMD)**         : `.venv\Scripts\activate.bat`
- **macOS / Linux**         : `source .venv/bin/activate`

### 3 — Install dependencies

```bash
pip install -r requirements.txt
```

### 4 — (Optional) Set your NCBI API key

Without a key the NCBI API is limited to **3 requests/second**.
With a free key you get **10 requests/second**.

Get your key at <https://www.ncbi.nlm.nih.gov/account/> → Settings → API Key Management.

Copy the example env file and add your key:

```bash
cp .env.example .env
# then edit .env and uncomment the NCBI_API_KEY line
```

Or set it directly in your shell:

```bash
export NCBI_API_KEY=your_key_here      # macOS / Linux
$env:NCBI_API_KEY = "your_key_here"   # Windows PowerShell
```

---

## Configure Claude Desktop

Edit `claude_desktop_config.json` (location depends on your OS):

| OS | Path |
|----|------|
| Windows | `%APPDATA%\Claude\claude_desktop_config.json` |
| macOS   | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Linux   | `~/.config/Claude/claude_desktop_config.json` |

Add the following block inside the `"mcpServers"` object:

```json
{
  "mcpServers": {
    "pubmed": {
      "command": "python",
      "args": ["C:/path/to/mcp-pubmed/main.py"],
      "env": {
        "NCBI_API_KEY": "your_key_here"
      }
    }
  }
}
```

> **Windows tip:** use forward slashes or double backslashes in the path.
> If `python` is not on your PATH, use the full path to your virtual environment:
> `"C:/path/to/mcp-pubmed/.venv/Scripts/python.exe"`

Restart Claude Desktop. You should see the 5 PubMed tools available.

---

## Configure Claude Code (VS Code / CLI)

Run this command from the project root:

```bash
claude mcp add pubmed python main.py
```

Or add it manually to your Claude Code settings (`.claude/settings.json`):

```json
{
  "mcpServers": {
    "pubmed": {
      "command": "python",
      "args": ["main.py"],
      "env": {
        "NCBI_API_KEY": "your_key_here"
      }
    }
  }
}
```

---

## Usage examples

Once connected, just ask Claude naturally:

```
Search for recent meta-analyses on SGLT2 inhibitors and heart failure.

Find the 5 latest meta-analyses on metformin and cancer.

Find all articles published by Topol EJ since 2020.

Get the abstract for PMID 33982811.

Is the full text of PMID 34591945 available?

Find articles related to PMID 31475795.
```

### PubMed query syntax

The `search_pubmed` tool accepts standard PubMed query syntax:

| Example | Meaning |
|---------|---------|
| `"heart failure"[MeSH]` | Exact MeSH term |
| `metformin[tiab]` | Word in title or abstract |
| `Smith J[au]` | Articles by author |
| `2020:2024[pdat]` | Publication date range |
| `"Randomized Controlled Trial"[pt]` | Filter by publication type |
| `AND`, `OR`, `NOT` | Boolean operators |

---

## Rate limits

| Situation | Limit |
|-----------|-------|
| No API key | 3 requests / second |
| With API key | 10 requests / second |

The server handles **429 rate-limit errors** and **5xx server errors** automatically with up to 3 retries and exponential back-off.

---

## Project structure

```
mcp-pubmed/
├── main.py            # MCP server — all tools defined here
├── requirements.txt   # Python dependencies
├── pyproject.toml     # Package metadata
├── .env.example       # Environment variable template
└── README.md
```

---

## How it works

```
Claude ──MCP── main.py ──HTTPS── NCBI E-utilities API
                                 ├── esearch.fcgi  (search)
                                 ├── efetch.fcgi   (fetch records / full text)
                                 └── elink.fcgi    (related articles)
```

1. Claude calls a tool (e.g. `search_pubmed`).
2. `main.py` builds a request to the appropriate NCBI endpoint.
3. The XML/JSON response is parsed and formatted as plain text.
4. Claude receives the result and presents it to you.

---

## Troubleshooting

**"No module named mcp"**
→ Make sure your virtual environment is activated and you ran `pip install -r requirements.txt`.

**"Could not connect to NCBI"**
→ Check your internet connection. NCBI is at `eutils.ncbi.nlm.nih.gov`.

**Rate limit errors (429)**
→ Add an NCBI API key (see above).

**Claude does not see the tools**
→ Check that the path in `claude_desktop_config.json` is absolute and correct.
→ Restart Claude Desktop after any config change.

---

## License

MIT — free for personal and commercial use.

---

## Acknowledgements

Built on the [NCBI E-utilities API](https://www.ncbi.nlm.nih.gov/books/NBK25499/) (free, no subscription required) and the [Model Context Protocol](https://modelcontextprotocol.io/) by Anthropic.
