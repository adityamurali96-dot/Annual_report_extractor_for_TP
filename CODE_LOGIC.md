# Annual Report Extractor - Code Logic Documentation

Complete line-by-line explanation of how the extraction pipeline works, from PDF upload to Excel output.

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [File-by-File Breakdown](#file-by-file-breakdown)
   - [app/config.py](#appconfigpy)
   - [app/main.py](#appmainpy)
   - [app/pdf_utils.py](#apppdf_utilspy)
   - [app/adobe_converter.py](#appadobe_converterpy)
   - [app/claude_parser.py](#appclaude_parserpy)
   - [app/extractor.py](#appextractorpy)
   - [app/docling_extractor.py](#appdocling_extractorpy)
   - [app/table_extractor.py](#apptable_extractorpy)
   - [app/excel_writer.py](#appexcel_writerpy)
   - [templates/index.html](#templatesindexhtml)
3. [Pipeline Flow](#pipeline-flow)
4. [Error Handling](#error-handling)
5. [Fallback Chains](#fallback-chains)

---

## Architecture Overview

```
User uploads PDF(s) via browser
        |
        v
[FastAPI /extract endpoint]
        |
        v
Step 0: classify_pdf() --> "text" / "scanned" / "vector_outlined"
        |                       |
        |                   If scanned/vector:
        |                   Adobe OCR convert --> searchable PDF
        |                       |
        v                       v
Step 1: Identify standalone financial statement pages
        |
        +--> Method 1: Claude API (if ANTHROPIC_API_KEY set)
        +--> Method 2: Regex title matching (fallback)
        +--> Method 3: Content-based keyword scoring (last resort)
        |
        v
Step 1b: Check for multiple P&L candidates, generate warnings
        |
        v
Step 2: Extract page headers for company name validation
        |
        v
Step 3: Extract P&L table data
        |
        +--> Primary: Docling (IBM) table extraction
        +--> Fallback: pymupdf4llm markdown table extraction
        |
        v
Step 4: Extract "Other Expenses" note breakup
        |
        +--> Find note page by note reference number
        +--> Extract note items (Docling -> text fallback)
        |
        v
Step 4b: Validate note totals against P&L figures
        |
        v
Step 5: Generate Excel with 5 sheets
        |
        v
Return .xlsx file to browser
```

---

## File-by-File Breakdown

### app/config.py

Central configuration loaded from environment variables. All settings have sensible defaults.

```
Line  3-4:   BASE_DIR / UPLOAD_DIR — project root and uploads folder
Line  8:     ANTHROPIC_API_KEY — Claude API key for page identification (optional)
Line 11-15:  Warn if API key doesn't start with "sk-ant-" (likely misconfigured)
Line 18-22:  Extraction settings:
             - MAX_UPLOAD_SIZE_MB (default 50)
             - MAX_PDF_PAGES (default 500)
             - OCR_DPI (default 150)
             - CLAUDE_MODEL (default claude-sonnet-4-5)
             - CLEANUP_AGE_SECONDS (default 3600)
Line 25-26:  ADOBE_CLIENT_ID / ADOBE_CLIENT_SECRET — for Adobe OCR (optional)
Line 29-30:  HOST / PORT — server bind settings
```

### app/main.py

FastAPI application — the entry point for all requests.

#### Startup & Routes (Lines 1-73)

```
Line 12-16:  ThreadPoolExecutor(max_workers=2) — runs blocking extraction
             in background threads so the async web server stays responsive
Line 42-48:  _cleanup_old_files() — deletes .xlsx files older than 1 hour
             from the uploads directory. Called on every /extract request.
Line 51-59:  GET / — serves the upload page. Passes two template variables:
             has_api_key (Claude configured?) and has_adobe_ocr (Adobe configured?)
Line 62-70:  GET /health — health check for Railway deployment monitoring
```

#### POST /extract (Lines 73-190)

The main extraction endpoint. Accepts 1-2 PDF files.

```
Line 82-84:  Validate file count (1-2 PDFs only)
Line 85:     Call _cleanup_old_files() to remove stale output files
Line 89-98:  For each uploaded file:
             - Validate it's a PDF by extension
             - Read in 1MB chunks (streaming) to avoid memory spikes
             - Reject files larger than MAX_UPLOAD_SIZE_MB
Line 114-116: Generate a short job_id (8 chars of UUID), save PDF to uploads/
Line 120-127: Run _run_extraction in the thread pool (non-blocking)
Line 128-130: On success, record the Excel path and download name
Line 138-148: Error handling:
             - ValueError (422) — known failures (no P&L found, etc.)
             - RuntimeError (500) — configuration issues (missing SDK)
             - Exception (500) — unexpected errors
Line 166-168: finally block — always delete the uploaded PDF after processing
Line 170-179: Single file → return Excel directly as FileResponse
Line 181-190: Multiple files → return JSON with download links
```

#### _run_extraction (Lines 206-526)

The core pipeline. Runs in a background thread.

```
Line 215-225: Import all modules lazily (inside function, not at module level)
              This prevents import errors from crashing the entire app.

--- STEP 0: PDF Classification (Lines 234-283) ---

Line 236:    classify_pdf(pdf_path) → "text", "scanned", or "vector_outlined"
Line 239-275: If scanned or vector_outlined:
              - If Adobe credentials available → convert via Adobe OCR API
              - If Adobe fails → raise ValueError with Adobe error message
              - If no Adobe credentials → raise ValueError immediately
                (fail fast — no point trying regex on empty text)
Line 277-283: Secondary scanned check for hybrid PDFs
              (is_scanned_pdf adds a warning about OCR accuracy)

--- STEP 1: Page Identification (Lines 288-379) ---

Three identification methods, tried in order:

Method 1 - Claude API (Line 295-334):
  - Sends page summaries to Claude Sonnet 4.5
  - Claude returns JSON: {pages: {pnl, balance_sheet, cash_flow, notes_start}}
  - Also returns company_name, currency, fiscal_years
  - Guardrail: checks if Claude picked a Table-of-Contents page
    (TOC pages mention all statement names and fool LLMs)

Method 2 - Regex (Line 337-338):
  - find_standalone_pages_regex() from extractor.py
  - Searches for P&L title patterns ("Statement of Profit and Loss", etc.)
  - Two passes: explicit "standalone" labels, then single-entity fallback

Method 3 - Content Scoring (Line 343-352):
  - Scores each page by P&L keyword density
  - Keywords: "revenue from operations" (+5), "profit before tax" (+5), etc.
  - Negative signals: "table of contents" (-8), "director" (-8)
  - Page must score >= 20 to be considered a P&L page
  - Guardrail: consolidated pages penalised (-30) in multi-section reports

Line 354-379: If no P&L page found after all 3 methods → raise ValueError
              with diagnostic info (which methods were tried, PDF type)

--- STEP 1b: Multiple Candidate Check (Lines 384-401) ---

Line 384:    find_all_standalone_candidates() scans ALL pages for P&L matches
Line 390:    compute_pnl_confidence() scores how certain we are:
             - 1 candidate → 100% (certain)
             - 2 candidates + Claude → 75%
             - 3+ candidates → low → warn user
Line 394-401: If multiple candidates, add warning with all page numbers

--- STEP 2: Extract Page Headers (Lines 406-408) ---

Line 406:    extract_page_headers() reads top 5 lines from each identified page
             These headers appear in the Validation sheet so users can verify
             the correct company/entity is being processed.

--- STEP 3: P&L Table Extraction (Lines 413-448) ---

Line 415:    Primary: extract_pnl_docling() — IBM Docling table extraction
             (Uses TableFormer model in ACCURATE mode)
Line 417-418: If Docling fails, log warning and set pnl = None
Line 421-428: Fallback: extract_pnl_from_tables() — pymupdf4llm markdown tables
Line 429-441: If both fail → raise ValueError
Line 443-446: Attach company_name and currency from Claude to the P&L data

--- STEP 4: Note Breakup Extraction (Lines 453-480) ---

Line 456:    Get note reference number for "Other expenses" from P&L
Line 459:    Get the actual matched label (could be "Administrative Charges")
Line 463-466: find_note_page() — searches for note heading "27. Other expenses"
             5 strategies tried in order of specificity:
             1. Note number + keyword on same line
             2. Note number at line start, keyword within ±4 lines
             3. Note number + keyword anywhere on same page
             4. Note heading only (no keyword required)
             5. Keyword on any page in notes section
Line 468-476: extract_note_docling() — Docling extracts note table items

--- STEP 4b: Validation (Lines 484-494) ---

Line 485:    validate_note_extraction() cross-checks:
             1. Note total (CY) vs P&L Other Expenses (CY)
             2. Note total (PY) vs P&L Other Expenses (PY)
             3. Sum of note items vs note total
             4. Note item count (informational)
Line 486-494: Log each check result, add warnings for FAILs

--- STEP 5: Excel Generation (Lines 498-526) ---

Line 498-513: Assemble all data into a single dict
Line 515:    create_excel() generates the 5-sheet workbook
Line 519-524: Clean up converted temp PDF if Adobe OCR was used
Line 526:    Return {excel_path, data, warnings}
```

### app/pdf_utils.py

PDF text extraction and classification utilities.

#### classify_pdf (Lines 126-242)

Determines PDF type by sampling pages from front, middle, and back.

```
Line 144-163: Sample ~20 pages spread across the document:
              - Front 3 pages (cover, TOC)
              - Middle 10 pages (where financials usually are)
              - Back quarter 6 pages (where notes usually are)

Line 169-214: For each sampled page, classify it:
              Check 1 (Line 173-177): word_count >= 20 → text_pages++
                (Has extractable text — normal PDF page)

              Check 2 (Line 179-192): content stream > 30KB → vector_pages++
                (Fonts converted to Bezier curves — looks like text but
                 page.get_text() returns nothing. Detected by measuring
                 the raw PDF content stream size — vector shapes are verbose.)
                 *** This check runs BEFORE the image check because
                 vector-outlined PDFs often contain small decorative
                 images (logos, borders) that would cause false "scanned"
                 classification if checked first. ***

              Check 3 (Line 195-214): large images > 50KB → image_pages++
                (Scanned/photographed pages. Only images > 50KB count
                 to filter out logos/watermarks/decorations.)

Line 224-239: Decision logic:
              - >= 50% text pages → "text" (normal PDF)
              - vector pages exist AND >= image pages → "vector_outlined"
              - image pages exist → "scanned"
              - < 30% text pages → "vector_outlined" (edge case fallback)
              - Default → "text" (let the pipeline try)
```

#### is_scanned_pdf (Lines 60-123)

Secondary scanned detection (used for hybrid PDF warning).

```
Line 73-90:  Samples from front (5), middle (10), back (5) of the document
Line 94-106: For each page: check word count and image presence
Line 114:    Returns True if >= 50% low-text AND >= 50% have images
```

#### is_page_scanned (Lines 48-57)

Per-page scanned check for hybrid PDFs.

```
Line 51-54:  A single page is "scanned" if:
             - word_count < 20 (little extractable text)
             - AND the page contains at least 1 image
```

#### extract_pdf_text (Lines 245-288)

Extracts text from all pages, with OCR fallback for scanned pages.

```
Line 252:    Determine if overall PDF is scanned
Line 256-258: For each page:
              1. Get text with page.get_text()
              2. If page is scanned (overall or per-page) AND < 20 words:
Line 263-271:    Try Tesseract OCR via PyMuPDF's get_textpage_ocr()
                 - language="eng", dpi=150
                 - full=False (only OCR image areas where no text exists)
                 - If OCR text is longer, use it instead
Line 272-273:    If Tesseract not available, catch exception and keep original text
```

### app/adobe_converter.py

Adobe PDF Services OCR integration for scanned/vector-outlined PDFs.

```
Line 20-22:  is_adobe_available() — checks if ADOBE_CLIENT_ID and SECRET are set
Line 25-124: convert_to_searchable_pdf(input_pdf_path) → str:
             1. Check credentials (raise if not configured)
             2. Lazy-import Adobe SDK (raise if not installed)
             3. Authenticate with ServicePrincipalCredentials
             4. Upload PDF to Adobe cloud
             5. Submit OCR job with params:
                - Locale: EN_US (works for Indian reports — financial terms are English)
                - Type: SEARCHABLE_IMAGE_EXACT (adds invisible text layer,
                  preserves original appearance pixel-perfect)
             6. Wait for result (SDK polls internally, typically 15-45 seconds)
             7. Download searchable PDF to a temp file
             8. Return temp file path (caller must clean up)
```

### app/claude_parser.py

Claude API integration for page identification.

#### IDENTIFY_PAGES_PROMPT (Lines 43-108)

The prompt sent to Claude. Key instructions:

```
- Distinguish multi-section (standalone vs consolidated) from single-entity reports
- P&L title variations: 11+ different title formats listed
- Handle scanned/OCR PDFs with garbled text
- Look for: P&L, Balance Sheet, Cash Flow, Notes start
- Extract: company name, currency unit, fiscal years, page headers
- Return JSON with 0-indexed page numbers
```

#### identify_pages (Lines 111-190)

```
Line 120-121: Extract text from all pages via extract_pdf_text()
Line 126-136: Build page summaries — for each page:
              - Take first 40 raw lines
              - Filter to 25 non-empty lines (skip blank OCR header lines)
              - Track empty pages
Line 140-149: If > 80% pages empty → raise ValueError
              (PDF is scanned/vector without successful OCR)
Line 151-152: Process in batches of 80 pages (to stay within Claude context limits)
Line 155-165: Send to Claude API:
              - Model: CLAUDE_MODEL (configurable, default sonnet 4.5)
              - max_tokens: 1024
              - timeout: 30 seconds
Line 167-170: Parse JSON response (handle markdown ```json``` wrapping)
Line 173-185: Merge batch results (prefer non-null values across batches)
```

### app/extractor.py

Regex/pattern-based extraction logic. Used as fallback and for validation.

#### P&L Title Matching (Lines 25-70)

```
Line 25-43:  _PNL_TITLE_REGEXES — 12 regex patterns covering:
             - Standard Indian GAAP: "Statement of Profit and Loss"
             - IFRS: "Statement of Profit or Loss"
             - Older formats: "Profit and Loss Account"
             - Non-profit: "Income and Expenditure Account"
             - Short-form: "P&L Statement"
             - Catch-all: "profit and loss" (close together)

Line 46-64:  _normalise_for_title_match() — robust text normalization:
             - Lowercase
             - Normalize Unicode dashes, quotes
             - Replace non-breaking spaces
             - Collapse OCR noise characters (|, _, ~)
             - Collapse whitespace (handles split titles across lines)
             - Treat slashes/hyphens as separators
```

#### Table-of-Contents Detection (Lines 73-130)

```
Line 73-130: _is_likely_toc_page(text) — heuristic check:
             1. Check header for TOC markers ("table of contents", "index", etc.)
             2. Count lines matching pattern: "Text ... PageNumber"
             3. Count lines with dotted leaders ("....")
             4. If >= 4 TOC entries AND >= 20% of lines → TOC page
             5. If >= 5 dotted lines → TOC page
```

#### Content-Based P&L Scoring (Lines 136-237)

```
Line 139-177: _PNL_CONTENT_KEYWORDS — weighted keywords:
              Very strong (5): "revenue from operations", "profit before tax"
              Strong (3-4): "employee benefits expense", "finance costs"
              Moderate (2): "other expenses", "current tax"
              Weak (1): "ebitda"

Line 180-185: _PNL_NEGATIVE_KEYWORDS — anti-signals:
              "director", "auditor", "governance", "chairman"

Line 188-237: _score_page_as_pnl(text, require_standalone):
              - Sum positive keyword scores
              - Subtract 8 for each negative keyword
              - +10 for recognized P&L title
              - -10 for short pages (< 200 chars)
              - -20 for TOC-like pages
              - If require_standalone: -30 for consolidated headers
              - If require_standalone: +15 for standalone labels
              Typical genuine P&L: 25+. Non-P&L: < 10.
```

#### find_standalone_pages (Lines 326-389)

Three-pass page identification:

```
Pass 1 (Line 339-349): Explicit "standalone" / "separate" labels
         Looks for pages with BOTH a P&L title AND a standalone label.
         These are the most reliable matches.

Pass 2 (Line 352-367): Single-entity fallback
         Only runs if Pass 1 found nothing AND no consolidated section exists.
         Matches any page with a P&L title (they're implicitly standalone).

Pass 3 (Line 374-386): Content scoring fallback
         Runs if Passes 1-2 found nothing.
         Scores all pages, picks best above threshold of 20.
         Guardrail: consolidated pages penalised when consolidated section exists.
```

#### find_note_page (Lines 556-658)

Five strategies to find a specific note number page:

```
Strategy 1 (Lines 578-592): Note number + keyword on SAME line
            e.g., "27. Other expenses" or "Note 27: Other expenses"

Strategy 2 (Lines 595-609): Note number at start, keyword nearby (±4 lines)
            e.g., Line N: "27." → Line N+3: "Other expenses"

Strategy 3 (Lines 612-627): Note number + keyword anywhere on same page
            Broader match — note heading and keyword on the same page.

Strategy 4 (Lines 630-645): Note heading only (no keyword required)
            For reports where the note is in a combined table.
            Matches "27. [any text starting with letter]".

Strategy 5 (Lines 648-655): Keyword only in notes section
            Last resort — just finds a page with "Other expenses" + "expense".
```

#### validate_note_extraction (Lines 734-799)

Cross-checks extracted note data against P&L figures:

```
Check 1: Note total (CY) vs P&L Other Expenses (CY)
Check 2: Note total (PY) vs P&L Other Expenses (PY)
Check 3: Sum of individual note items vs Note total
Check 4: Note item count (informational, always passes if > 0)

Tolerance is dynamic: 0.1% of P&L value (minimum 1.0)
This handles rounding differences between notes and P&L.
```

### app/docling_extractor.py

IBM Docling-based table extraction — the primary extraction method.

#### Converter Setup (Lines 40-105)

```
Line 21-23:  Two converters cached: _converter (no OCR) and _converter_ocr
             Thread-safe initialization via _converter_lock.

Line 40-105: _get_converter(use_ocr):
             - Creates Docling DocumentConverter with:
               - do_table_structure=True
               - TableFormerMode.ACCURATE (high-quality model)
               - do_cell_matching=True
             - If OCR: configures EasyOCR with English language
             - Converter is cached globally (heavy initialization, ~5 seconds)
```

#### P&L Extraction (Lines 505-608)

```
Line 519-522: Create temp PDF with only target pages (P&L + next page)
              P&L statements often span 2 pages.
Line 524-530: Extract tables from temp PDF via Docling
Line 531-533: If no tables without OCR → retry WITH OCR enabled
Line 541-542: _find_best_pnl_table() — scores tables by keyword matches
Line 549:     _extract_pnl_from_df() — extracts P&L items from DataFrame:
              1. Identify label column (skip serial number columns)
              2. Identify value columns (current year, previous year)
              3. Match each row label against PNL_ITEMS patterns
              4. Extract note references from intermediate columns
Line 556-566: If < 5 items found → try other tables too
Line 569-578: Fallback: extract note reference from raw PDF text
              (Docling sometimes misses note ref columns)
```

#### Column Identification (Lines 339-386)

```
Line 340-386: _identify_value_columns_df(df):
              1. Detect serial number column (roman numerals, digits, letters)
              2. Count numeric values per column (skip note references)
              3. Value columns must have >= 15% numeric cells
              4. Last two value columns are current year (2nd-to-last)
                 and previous year (last)
```

#### Note Extraction (Lines 1001-1116)

Multi-layered fallback system:

```
Layer 1 (Line 1057-1059): Targeted extraction from best-scoring note table
         Uses _extract_note_items_from_df()

Layer 2 (Line 1068-1082): Combined-table extraction
         Searches ALL tables for the note heading, extracts section

Layer 3 (Line 1088-1097): Full-table fallback
         Extracts ALL expense-like rows from all tables

Layer 4 (Line 1102-1110): Text-based fallback
         Falls back to raw PDF text parsing (no Docling)
```

### app/table_extractor.py

pymupdf4llm-based extraction — used as fallback when Docling fails.

```
Line 54-81:  _extract_markdown_tables():
             Tries 3 pymupdf4llm strategies in order:
             1. 'lines_strict' — strictest table detection
             2. 'lines' — relaxed line-based detection
             3. 'text' — text-based table detection

Line 84-114: _parse_markdown_tables():
             Parses markdown pipe-delimited tables:
             - Skip separator rows (|---|---|)
             - Split cells by pipe character
             - Group consecutive table rows

Line 258-338: extract_pnl_from_tables():
              Same logic as Docling extractor but using markdown tables:
              1. Extract tables from P&L page + next page
              2. Find best P&L table by keyword scoring
              3. Identify value columns
              4. Match rows to P&L items by label patterns
```

### app/excel_writer.py

Generates the 5-sheet Excel workbook.

#### Sheet 1: P&L - Extracted (Lines 57-127)

```
Layout:
  Row 1:   Company Name - Standalone P&L (merged, title font)
  Row 2:   Source: Annual Report | Currency unit
  Row 4:   Headers: Particulars | FY Current | FY Previous | YoY Change
  Rows 5+: P&L line items with values and formatting

PNL_ROWS defines the output structure (Lines 57-81):
  - Section headers (INCOME, EXPENSES) with blue background
  - Line items mapped to extraction keys
  - Total rows with bold font and double border
  - YoY Change formula: =IF(C{r}=0,"N/A",(B{r}-C{r})/ABS(C{r}))
```

#### Sheet 2: Operating Metrics (Lines 133-210)

```
Computed metrics from P&L data:
  - Revenue, Income, Operating Expenses
  - EBIT (Operating Profit) = Revenue - (Emp + CoP + Dep + OtherExp)
  - EBITDA = EBIT + Depreciation
  - All margins as percentages of Revenue
  - YoY change for absolute values, bps change for percentages
  - Highlighted rows (green) for key profitability metrics
```

#### Sheet 3: Other Expenses Breakup (Lines 217-308)

```
  - Lists all note line items with CY, PY, YoY Change, % of Revenue
  - Total row detected by matching against P&L Other Expenses value
  - Sub-items indented and italicized
  - "TOP 3 EXPENSE HEADS" section at bottom (orange highlight)
```

#### Sheet 4: Validation (Lines 315-429)

```
Section 1 - Page Headers:
  Shows the first few lines from each identified page
  User can verify the correct company is being processed

Section 2 - P&L Cross-Checks:
  1. Total Income - Total Expenses = PBT
  2. PBT - Tax = PAT
  3. Operating Profit calculation verification
  4. EBITDA calculation verification
  Each shows PASS/FAIL with computed vs reported values

Section 3 - Note Validation:
  Shows results from validate_note_extraction()
```

#### Sheet 5: Extraction Info (Lines 435-464)

```
Metadata for audit:
  - Extraction timestamp, Job ID
  - Company name, currency unit
  - Fiscal years
  - Page numbers for each section
  - Identification method (Claude API vs Regex/Scoring)
  - All warnings generated during extraction
```

### templates/index.html

Single-page web UI with drag-and-drop upload.

```
Lines 17-57:  Upload section:
              - Drag & drop zone (or click to browse)
              - Max 2 files
              - File list with size and remove button
              - "Extract Financials" button
              - Info box: what gets extracted
              - Warning boxes: API key not set, Adobe OCR not configured

Lines 60-83:  Processing section:
              - Spinner animation
              - Elapsed time counter
              - 4-step progress indicator: Upload → Identify → Extract → Excel

Lines 87-97:  Warning/error banners for extraction results

Lines 105-333: JavaScript:
               - handleFiles() — validates PDF extension and max 2 files
               - extractBtn click → POST /extract with FormData
               - Shows elapsed time during processing
               - Handles single file (direct blob download) vs
                 multi-file (JSON with download links)
               - showWarnings() — displays review-required warnings
               - showError() — displays error message with retry button
```

---

## Pipeline Flow

### For a Normal Text PDF

```
1. Upload → save to uploads/{job_id}.pdf
2. classify_pdf() → "text"
3. identify_pages() via Claude API → {pnl: 45, bs: 42, cf: 48, notes_start: 52}
4. extract_page_headers() → first 5 lines from each page
5. extract_pnl_docling() → {items: {Revenue: ..., Profit: ...}, note_refs: {Other expenses: "27"}}
6. find_note_page("27") → page 73
7. extract_note_docling(page 73, "27") → [list of expense items]
8. validate_note_extraction() → [PASS, PASS, PASS]
9. create_excel() → uploads/{job_id}_output.xlsx
10. Return .xlsx file
```

### For a Scanned/Vector-Outlined PDF (with Adobe OCR)

```
1. Upload → save to uploads/{job_id}.pdf
2. classify_pdf() → "scanned" or "vector_outlined"
3. Adobe OCR: convert_to_searchable_pdf() → /tmp/xxx_searchable.pdf
4. Pipeline continues using the converted PDF (same as normal flow)
5. Clean up converted temp PDF after extraction
```

### For a Scanned PDF (without Adobe OCR)

```
1. Upload → save to uploads/{job_id}.pdf
2. classify_pdf() → "scanned"
3. Adobe not configured → FAIL FAST
   Error: "This PDF is scanned/image-based — it contains no extractable text.
   To process this type of PDF, configure Adobe OCR..."
```

---

## Error Handling

| Error | HTTP Status | When |
|-------|-------------|------|
| `ValueError` | 422 | No P&L page found, no tables extracted, note extraction failed |
| `RuntimeError` | 500 | Missing SDK, bad credentials, configuration errors |
| `Exception` | 500 | Unexpected errors (logged with full traceback) |

All error messages include diagnostic context:
- PDF type (text/scanned/vector)
- Methods tried (Claude API, regex, content scoring)
- Whether Adobe OCR was attempted

---

## Fallback Chains

### Page Identification
```
Claude API → Regex title matching → Content keyword scoring → ValueError
```

### P&L Table Extraction
```
Docling (ACCURATE mode) → pymupdf4llm markdown tables → ValueError
```

### Note Table Extraction
```
Docling targeted extraction
  → Combined-table section extraction
    → Full-table expense row extraction
      → Raw text parsing
        → Empty result (warning, no error)
```

### Note Reference Finding
```
Same-line match → Nearby-line match → Same-page match →
  Heading-only match → Keyword-only match → None
```

### OCR for Scanned Pages
```
Adobe PDF Services OCR → Tesseract via PyMuPDF → Raw text (may be empty)
```

---

## Key Design Decisions

1. **Lazy imports everywhere** — Heavy packages (Docling, anthropic, Adobe SDK) are imported inside functions, not at module level. This means the app starts fast and doesn't crash if optional packages are missing.

2. **Thread pool for extraction** — `ThreadPoolExecutor(max_workers=2)` runs blocking PDF extraction in background threads, keeping the async FastAPI server responsive.

3. **Vector check before image check** — In `classify_pdf`, vector content streams are checked BEFORE images. This prevents vector-outlined PDFs (which often contain small decorative images) from being misclassified as "scanned".

4. **Consolidated guardrails** — Content scoring penalises (-30) pages with "consolidated" in headers when the document has both standalone and consolidated sections. This prevents accidentally extracting from the consolidated P&L.

5. **Fail fast for non-text PDFs** — If a PDF is scanned/vector-outlined and Adobe OCR isn't configured, the pipeline fails immediately with clear instructions instead of wasting time trying 3 identification methods on empty text.

6. **Dynamic validation tolerance** — 0.1% of the P&L value (minimum 1.0) instead of a fixed tolerance. This handles the wide range of currency units (Rs in Lakhs vs Rs in Crores) without false positives.
