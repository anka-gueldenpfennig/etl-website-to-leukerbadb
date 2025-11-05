from supabase import create_client
import re
from dotenv import load_dotenv
import os
from pathlib import Path

# ---------- CONFIG & DATABASE CONNECTION ----------------
# Resolve the project root (two levels up if needed)
project_root = Path(__file__).resolve().parent.parent
env_path = project_root / ".env"

# get env to connect to supabase
if env_path.exists():
    from dotenv import load_dotenv
    load_dotenv()

# read config
url = os.environ["SUPABASE_URL"]
key = os.environ["SUPABASE_KEY"]

# connect to supabase LeukerbaDB project
supabase = create_client(url, key)

# -------------- METHODS ----------------
# "tidy" query terms - redirect and remove trigger words
def tidy_query(q: str):
    # remove trigger words from search terms in all their variations
    if not re.search(r'\b\w*skitour\w*\b', q, flags=re.IGNORECASE): # hardcode ignore skitour as its own term
        if re.search(r'\b\w*(preis|tour|trail)\w*\b', q, flags=re.IGNORECASE):
            q = re.sub(r'preise|preis|tour|touren|trails|trail', '', q, flags=re.IGNORECASE).strip() # when adding here, remember to put longest version of a word first

    print(q)

    return q

# limit result types based on query trigger words
def query_limit_results(res: list, q: str) -> list:
    # if the query contains "Preis", return only table components
    if re.search(r'\b\w*preis\w*\b', q, flags = re.IGNORECASE):
        comp = 'table'

    # if the query contains tour/trail/route etc, return urls that match OA tours and tour overview page
    # except skitours (hardcode ignore)
    elif not re.search(r'\b\w*skitour\w*\b', q, flags=re.IGNORECASE) and re.search(r'\b\w*(tour|trail)\w*\b', q, flags = re.IGNORECASE):
        # match url
        comp = 'tour_url'

    else:
        comp = None

    # empty results list
    final_results = []

    # only keep relevant components if there is a trigger word
    if comp == 'table':
        for result in res:
            if result.get("component") == comp:
                final_results.append(result)

    elif comp == 'tour_url':
        for result in res:
            # pull the url
            url = result.get("url")
            if re.search('.*/tour/.*|.*#tour', url):
                final_results.append(result)

    # otherwise, just return results as they are
    else:
        final_results = res

    # but if the result is empty after component filtering, return original results (so we don't return nothing when there is relevant content)
    if not final_results:
        final_results = res

    return final_results

# tidy up results return
def dedupe_results(results: list) -> list:
    final_results = []

    for result in results:
        # only proceed if no exact match (if the exact item is already in final results, don't add it again)
        # also remove breadcrumb components
        if result not in final_results and result.get("component") != "breadcrumb":
            # then check for partial match (if url, title and summary are the same, it's the same result just from different components/pages)
            # get content for new item
            url = result.get("url")
            title = result.get("title")
            summary = result.get("summary")

            # start with assumption that result is not in final results yet
            skip = False
            # iterate existing record of final results
            for res in final_results:
                if url == res.get("url") and title == res.get("title") and summary == res.get("summary"):
                    skip = True # if the new result matches an existing result, it will not be added again

            # if the result has not been found after going through final results record, add it
            if not skip:
                final_results.append(result)

    return final_results


# --------------- MAIN CODE ---------------
q = "Bike"

# remove
q_send = tidy_query(q)

res = supabase.rpc('search_web_content', {'q': q_send, 'limit_n': 500, 'offset_n': 0}).execute() # initially high limit because we cut the length after deduplication + prio

# if the result is not empty, deduplicate and tidy results
if res.data and res.data != []:
    final_results = query_limit_results(res.data, q) # limit components based on trigger words
    final_results = dedupe_results(final_results) # deduplicate remaining results
    final_results = final_results[:10] # limit to 10 results at the end

else:
    final_results = []

# print output
for row in final_results or []:
    print(row)

print("compare to")

for row in res.data:
    print(row)
