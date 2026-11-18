-- 0003: knowledge base — crawled pages, their chunks + embeddings, and schema.org products.
--
-- Written by the crawler (crawler.py) and read by retrieval (retrieval.py), both as the API role
-- inside db.tenant_tx(), so the same row-level security as 0001 applies to every table here.
-- Crawled text is untrusted data: it is only ever shown to the model inside a fenced block
-- (see grounding.py), never treated as instructions.

CREATE EXTENSION IF NOT EXISTS vector;

-- One crawl run. Its counters are what a crawl-progress screen (roadmap step 7) will read.
CREATE TABLE crawl_jobs (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id       uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
  status          text NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'done', 'failed')),
  pages_found     integer NOT NULL DEFAULT 0,
  pages_indexed   integer NOT NULL DEFAULT 0,    -- new or changed pages (re-chunked)
  pages_unchanged integer NOT NULL DEFAULT 0,    -- same content as last time: nothing re-embedded
  pages_skipped   integer NOT NULL DEFAULT 0,    -- robots.txt, noindex, not HTML, duplicate canonical
  pages_failed    integer NOT NULL DEFAULT 0,    -- network/HTTP errors (the old copy is kept)
  pages_removed   integer NOT NULL DEFAULT 0,    -- gone from the site since the last crawl
  chunks_embedded integer NOT NULL DEFAULT 0,
  embed_tokens    integer NOT NULL DEFAULT 0,
  error           text CHECK (length(error) <= 500),
  started_at      timestamptz NOT NULL DEFAULT now(),
  finished_at     timestamptz
);
CREATE INDEX crawl_jobs_tenant_recent_idx ON crawl_jobs (tenant_id, started_at DESC);

CREATE TABLE kb_pages (
  id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id    uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
  url          text NOT NULL CHECK (length(url) <= 2000),
  title        text NOT NULL DEFAULT '' CHECK (length(title) <= 300),
  -- 'article' (schema.org Article/BlogPosting...): explains topics; its examples aren't company facts.
  kind         text NOT NULL DEFAULT 'page' CHECK (kind IN ('page', 'article')),
  content_hash text NOT NULL,                    -- of the extracted text + products: unchanged = skip
  last_job_id  uuid,                             -- the last crawl that saw this page
  fetched_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, url),
  UNIQUE (id, tenant_id)                         -- target of the composite FKs below
);

CREATE TABLE products (
  id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id    uuid NOT NULL,
  page_id      bigint NOT NULL,
  name         text NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
  url          text NOT NULL CHECK (length(url) <= 2000),
  description  text NOT NULL DEFAULT '' CHECK (length(description) <= 1000),
  image_url    text CHECK (length(image_url) <= 2000),
  brand        text CHECK (length(brand) <= 120),
  price        text CHECK (length(price) <= 20),
  currency     text CHECK (length(currency) <= 8),
  availability text CHECK (length(availability) <= 40),
  rating       real CHECK (rating BETWEEN 0 AND 5),
  review_count integer CHECK (review_count >= 0),
  UNIQUE (id, tenant_id),
  FOREIGN KEY (page_id, tenant_id) REFERENCES kb_pages (id, tenant_id) ON DELETE CASCADE
);
CREATE INDEX products_page_idx ON products (page_id);

-- Bounded by the API's chunker (chunker.MAX_CHARS); the CHECKs are a last line of defence.
CREATE TABLE kb_chunks (
  id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id  uuid NOT NULL,
  page_id    bigint NOT NULL,
  product_id bigint,                             -- set for a product's own summary chunk
  ord        integer NOT NULL,
  heading    text NOT NULL DEFAULT '' CHECK (length(heading) <= 300),
  content    text NOT NULL CHECK (length(content) BETWEEN 1 AND 4000),
  text_hash  text NOT NULL,                      -- of the embedded text: identical text reuses its vector
  embedding  vector(1024) NOT NULL,              -- baai/bge-m3
  tsv        tsvector GENERATED ALWAYS AS (to_tsvector('english', heading || ' ' || content)) STORED,
  FOREIGN KEY (page_id, tenant_id) REFERENCES kb_pages (id, tenant_id) ON DELETE CASCADE,
  FOREIGN KEY (product_id, tenant_id) REFERENCES products (id, tenant_id) ON DELETE CASCADE
);
CREATE INDEX kb_chunks_page_idx ON kb_chunks (page_id, ord);
CREATE INDEX kb_chunks_hash_idx ON kb_chunks (tenant_id, text_hash);
CREATE INDEX kb_chunks_tsv_idx ON kb_chunks USING gin (tsv);
-- Approximate nearest neighbour. With many tenants in one index, retrieval turns on pgvector's
-- iterative scan so the RLS tenant filter can't starve a small tenant of results.
CREATE INDEX kb_chunks_embedding_idx ON kb_chunks USING hnsw (embedding vector_cosine_ops);

ALTER TABLE crawl_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE crawl_jobs FORCE ROW LEVEL SECURITY;
ALTER TABLE kb_pages   ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb_pages   FORCE ROW LEVEL SECURITY;
ALTER TABLE products   ENABLE ROW LEVEL SECURITY;
ALTER TABLE products   FORCE ROW LEVEL SECURITY;
ALTER TABLE kb_chunks  ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb_chunks  FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation ON crawl_jobs
  USING (tenant_id = current_tenant_id()) WITH CHECK (tenant_id = current_tenant_id());
CREATE POLICY tenant_isolation ON kb_pages
  USING (tenant_id = current_tenant_id()) WITH CHECK (tenant_id = current_tenant_id());
CREATE POLICY tenant_isolation ON products
  USING (tenant_id = current_tenant_id()) WITH CHECK (tenant_id = current_tenant_id());
CREATE POLICY tenant_isolation ON kb_chunks
  USING (tenant_id = current_tenant_id()) WITH CHECK (tenant_id = current_tenant_id());

GRANT SELECT, INSERT, UPDATE ON crawl_jobs TO easychat_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON kb_pages, products, kb_chunks TO easychat_app;
