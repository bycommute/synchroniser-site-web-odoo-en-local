# Odoo Local Sync

Odoo Local Sync synchronizes website content between Odoo and local files, with a Git-like workflow:

1. pull the exact Odoo record before editing,
2. edit locally,
3. validate and diff,
4. push only the targeted file.

It was created so humans and AI coding agents can work on Odoo website pages, products, blog posts, QWeb views, categories, menus and images without editing blindly in production.

## AI Agent Contract

If you are an AI agent reading this README, follow these rules exactly.

Before any Odoo operation, check whether `.env` exists and contains:

```bash
ODOO_URL=
ODOO_DB=
ODOO_USERNAME=
ODOO_API_KEY=
```

If one is missing, ask the user for it. Do not guess credentials, database names or production URLs.

Before editing any Odoo-backed file, always pull the exact record:

```bash
odoo-sync pull pages --ids 74 --force
odoo-sync pull blog-posts --ids 945 --force
odoo-sync pull products --ids 371 --force
```

Use `--force` only before editing, when you intentionally want the current Odoo version to replace the local copy. Do not use it over unsaved local work.

Push only the file you touched:

```bash
odoo-sync validate pages/74-my-page.xml
odoo-sync diff pages/74-my-page.xml
odoo-sync push pages/74-my-page.xml --no-commit
```

Avoid category-wide pushes unless the user explicitly asked for a prepared batch:

```bash
odoo-sync push --category products
odoo-sync push --category all
```

For translated files, never change the source language while working on a translation. A file named `371-product.en_US.html` must have `lang: en_US` in its `ODOO-SYNC-METADATA` header. The validator and push guard enforce this by default.

For destructive actions, run the dry-run first:

```bash
odoo-sync delete pages/123-old-page.xml
odoo-sync delete pages/123-old-page.xml --apply
```

## Installation

From a project where you want Odoo content to live:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install git+https://github.com/bycommute/synchroniser-site-web-odoo-en-local.git
odoo-sync init
```

Then edit `.env`:

```bash
ODOO_URL=https://your-company.odoo.com
ODOO_DB=your-database-name
ODOO_USERNAME=you@example.com
ODOO_API_KEY=your-odoo-api-key
ODOO_WEBSITE_ID=1
ODOO_LANG=fr_FR
ODOO_SOURCE_LANG=fr_FR
ODOO_LANGS=fr_FR,en_US,de_DE,es_ES,it_IT,nl_BE
```

Test the connection:

```bash
odoo-sync test
odoo-sync langs
```

## What It Syncs

| Category | Odoo model | Local folder | Main field |
|---|---|---|---|
| `pages` | `website.page` | `pages/` | `arch_db` |
| `blog-posts` | `blog.post` | `blog-posts/` | `content` |
| `products` | `product.template` | `products/` | `website_description` |
| `dynamic-pages` | `ir.ui.view` | `dynamic-pages/` | `arch_db` |
| `installations-catalog` | `ir.ui.view` | `installations-catalog/` | `arch_db` |
| `employees` | `res.partner` | `employees/` | `website_description` |
| `categories` | `product.public.category` | `categories/` | `website_footer` |
| `menus` | `website.menu` | `menus/` | `mega_menu_content` |
| images | `ir.attachment` | `images/attachments/` | binary + metadata sidecar |

## Standard Workflow

List records:

```bash
odoo-sync list pages --limit 20
odoo-sync list products --limit 20
```

Pull one record:

```bash
odoo-sync pull pages --ids 74 --force
```

Pull all active languages:

```bash
odoo-sync pull products --ids 371 --all-langs --force
```

Validate and compare:

```bash
odoo-sync validate products/my-category/371-product.html
odoo-sync diff products/my-category/371-product.html
```

Push one file:

```bash
odoo-sync push products/my-category/371-product.html --no-commit
```

Create a new unpublished Odoo page from a local file:

```bash
odoo-sync push pages/new-page.xml --create --no-commit
```

## QWeb and Odoo HTML Rules

For `website.page`, raw snippet HTML is accepted and wrapped into a website QWeb template on push. A full page template should look like:

```xml
<t t-name="website.my_page">
  <t t-call="website.layout">
    <div id="wrap" class="oe_structure">
      <section class="s_text_block" data-snippet="s_text_block" data-name="Text">
        <div class="container">
          <h1>Title</h1>
        </div>
      </section>
    </div>
  </t>
</t>
```

Prefer Odoo-compatible snippets:

- Use `section` blocks with `data-snippet` and `data-name`.
- Keep styling scoped to your section classes.
- Avoid global CSS targeting `body`, `html`, `.container`, `.row` or `.col`.
- Avoid inline scripts, `onclick` handlers and `javascript:` URLs in content fields.
- For translations, translate text and metadata; keep the tag structure aligned with the source file.

## Safety Rails

Default guards:

```bash
SYNC_HTML_TRANSLATION_DIRECT_WRITE_FALLBACK=0
SYNC_ALLOW_DESCRIPTION_SALE_WRITES=0
SYNC_ALLOW_PRODUCT_STRUCTURAL_WRITES=0
SYNC_ALLOW_METADATA_FALSE_CLEARS=0
SYNC_ENFORCE_FILE_LANG_MATCH=1
```

Meaning:

- product price, internal reference, category and publication fields are blocked by default,
- `description_sale` writes are blocked by default because they can affect quote lines,
- a metadata value `False` will not clear text fields by accident,
- a translated filename must match the header language,
- direct translated HTML writes are disabled unless explicitly allowed.

## Images

Pull images with metadata sidecars:

```bash
odoo-sync images-pull --res-model product.template --res-id 371
```

Push metadata or changed binaries:

```bash
odoo-sync images-push images/attachments/123-image.webp --dry-run
odoo-sync images-push images/attachments/123-image.webp
```

## Redirections

Export redirects:

```bash
odoo-sync redirects-pull --excel redirections.xlsx
```

Import redirects safely:

```bash
odoo-sync redirects-push --excel redirections.xlsx --dry-run
odoo-sync redirects-push --excel redirections.xlsx
```

If your Excel contains absolute URLs from your own domain, configure:

```bash
ODOO_REDIRECT_LOCAL_HOSTS=example.com,www.example.com
```

## Smoke Tests

Local unit tests:

```bash
python -m pytest
```

Dry-run smoke tests:

```bash
python -m odoo_local_sync.smoke_test_sync_engine --target all
```

Real smoke tests create temporary unpublished Odoo records, verify them through the API, then clean up:

```bash
python -m odoo_local_sync.smoke_test_sync_engine --target all --apply
```

Only run `--apply` after `odoo-sync test` succeeds and the user confirms that temporary Odoo records are acceptable.
