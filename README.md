# LeukerbaDB Web Scraper

A Python-based scraper for leukerbad.ch, designed to extract structured website content and sync it into a Postgres database hosted on Supabase.

The goal of this project is to create a clean, queryable dataset for Leukerbad’s tourism website, supporting internal content search and future AI-based applications.



##### Overview

The scraper uses BeautifulSoup for HTML parsing and a modular set of extractor functions to handle the various component types found on the website — including tiles, image-text sections, offers, FAQs, and more.

Each extractor is responsible for normalizing and structuring its respective content type into a consistent record format, which is then persisted to the database.



Content is upserted into Supabase, with automatic de-duplication and hash-based change detection, so only modified elements are written back.



##### Key Features

###### Component-aware extraction

Parses Leukerbad’s CMS-generated layouts (tiles, sliders, offers, FAQs, etc.) into structured JSON-like records.



###### Change tracking

Each record includes a content\_hash and updated\_at timestamp for version control and incremental updates.



###### Clean data model

Stores records in a single web\_content table with fields such as:

page\_url, page\_title, component, group\_id

title, summary, cta\_url, price\_text

phone\_display, phone\_e164

content\_hash, updated\_at, scraped\_at



###### Upsert logic

Uses Supabase’s upsert with conflict keys (page\_url, component, group\_id, item\_index) to prevent duplication.



###### Environment-based configuration

Connection details (SUPABASE\_URL, SUPABASE\_KEY) are stored securely in a .env file.



##### Setup

1\. Requirements

pip install beautifulsoup4 python-dotenv supabase openai requests



2\. Environment Variables

Create a .env file in the project root:



SUPABASE\_URL=https://your-project.supabase.co

SUPABASE\_KEY=your-anon-or-service-role-key



3\. Run the Scraper

python scrape\_leukerbad.py



This script will:



1. Fetch pages from leukerbad.ch
2. Extract structured content into records
3. Upsert new or modified entries into Supabase



##### Database Schema



The main table is web\_content, created as:



create table if not exists web\_content (

&nbsp; id uuid primary key default gen\_random\_uuid(),

&nbsp; page\_url text not null,

&nbsp; page\_title text,

&nbsp; component text not null,

&nbsp; group\_id text,

&nbsp; item\_index int default 0,

&nbsp; title text,

&nbsp; summary text,

&nbsp; cta\_url text,

&nbsp; price\_text text,

&nbsp; phone\_display text,

&nbsp; phone\_e164 text,

&nbsp; content\_hash text not null,

&nbsp; updated\_at timestamptz,

&nbsp; constraint uniq\_content unique (page\_url, component, group\_id, item\_index)

);



Runs on schedule once per week (Monday 8 am UTC) via GitHub Actions. 

