import sqlite3
import json
import os
from sentence_transformers import SentenceTransformer, CrossEncoder, util
import torch
import numpy as np
from typing import Dict, List, Optional

# --- CONFIGURATION ---
DB_FILE = "grant_matches.db"
EMBEDDINGS_FILE = "grant_embeddings.json"
RETRIEVER_MODEL = 'all-MiniLM-L6-v2'
RERANKER_MODEL = 'cross-encoder/ms-marco-MiniLM-L-6-v2'
TOP_N_MATCHES = 20
# UPDATED: Increased initial pool to ensure we find a more diverse set of foundations
CANDIDATE_POOL_SIZE = 10000 

# --- Scoring Weights ---
WEIGHT_AI_SIMILARITY = 0.60
WEIGHT_ASSET_SIZE = 0.15
WEIGHT_GIVING_GROWTH = 0.10
WEIGHT_GEOGRAPHY = 0.10
WEIGHT_NATIONAL_GIVER = 0.05

def get_reviewed_eins(cursor):
    """Fetches all foundation EINs that have already received feedback."""
    cursor.execute("SELECT foundation_ein FROM feedback")
    return {row[0] for row in cursor.fetchall()}

def save_feedback(cursor, feedback_data):
    """Saves user feedback to the database."""
    cursor.executemany("INSERT OR REPLACE INTO feedback (foundation_ein, status) VALUES (?, ?)", feedback_data)
    print(f"\nSaved feedback for {len(feedback_data)} foundation(s).")

def calculate_giving_growth_score(cursor, ein: str) -> float:
    """Analyzes grant count over time to score giving velocity."""
    cursor.execute("SELECT f.formation_year, COUNT(g.id) FROM grants g JOIN foundations f ON g.foundation_ein = f.ein WHERE g.foundation_ein = ? GROUP BY f.formation_year ORDER BY f.formation_year DESC LIMIT 3", (ein,))
    history = cursor.fetchall()
    if len(history) < 2:
        return 50.0 # Neutral score
    latest_count = history[0][1]
    previous_count = history[1][1]
    if latest_count > previous_count:
        return 100.0 # High score for positive growth
    elif latest_count == previous_count:
        return 75.0 # Good score for stability
    else:
        return 40.0 

def calculate_asset_score(cursor, ein: str) -> float:
    """Scores a foundation based on its asset size using a log scale for better distribution."""
    cursor.execute("SELECT end_of_year_assets FROM foundations WHERE ein = ?", (ein,))
    assets_row = cursor.fetchone()
    assets = float(assets_row[0] or 1)
    score = min(100, 10 * np.log10(assets))
    return max(0, score)

def is_national_giver(cursor, ein: str) -> bool:
    """Checks if a foundation gives grants outside its home state."""
    cursor.execute("SELECT state FROM foundations WHERE ein = ?", (ein,))
    f_state_row = cursor.fetchone()
    if not f_state_row or not f_state_row[0]: return False
    foundation_state = f_state_row[0].upper()
    cursor.execute("SELECT DISTINCT recipient_state FROM grants WHERE foundation_ein = ? AND recipient_state IS NOT NULL", (ein,))
    recipient_states = {row[0].upper() for row in cursor.fetchall()}
    return any(state != foundation_state for state in recipient_states)

def find_past_funders(cursor, charity_ein: str) -> List[str]:
    """Finds foundations that have previously donated to the given charity EIN."""
    cursor.execute("SELECT DISTINCT foundation_ein FROM grants WHERE recipient_ein_matched = ?", (charity_ein,))
    return [row[0] for row in cursor.fetchall()]

def find_top_matches():
    """
    Finds top matches using a new multi-factor "Opportunity Score" and prioritizing past funders.
    """
    if not all(os.path.exists(f) for f in [DB_FILE, EMBEDDINGS_FILE]):
        print(f"Error: Make sure '{DB_FILE}' and '{EMBEDDINGS_FILE}' exist.")
        return

    print("--- AI Grant Matchmaker (Advanced Scoring) ---")
    
    # --- 1. Get User Input ---
    print("\nPlease enter your charity's mission statement or project description:")
    charity_profile = input("> ")
    print("Please enter your charity's EIN:")
    charity_ein = input("> ")
    print("Please enter your charity's city:")
    charity_city = input("> ").upper()
    print("Please enter your charity's state (e.g., CA):")
    charity_state = input("> ").upper()

    if not all([charity_profile, charity_ein, charity_city, charity_state]):
        print("Missing input. Exiting.")
        return

    # --- 2. Load Models, Data, and Find Past Funders ---
    print("\nLoading AI models and embedding data...")
    retriever = SentenceTransformer(RETRIEVER_MODEL)
    reranker = CrossEncoder(RERANKER_MODEL)
    with open(EMBEDDINGS_FILE, 'r') as f: data = json.load(f)
    grant_ids = data['grant_ids']
    grant_embeddings = torch.tensor(data['embeddings'])
    print("Models and data loaded successfully.")

    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    past_funder_eins = find_past_funders(cursor, charity_ein)
    reviewed_eins = get_reviewed_eins(cursor)
    eins_to_exclude = set(past_funder_eins) | reviewed_eins
    print(f"Found {len(past_funder_eins)} past funder(s) and {len(reviewed_eins)} previously reviewed foundation(s).")

    # --- 3. AI Matching Process ---
    print("\n--- Finding new prospects with AI... ---")
    query_embedding = retriever.encode(charity_profile, convert_to_tensor=True)
    retrieval_hits = torch.topk(util.cos_sim(query_embedding, grant_embeddings), k=CANDIDATE_POOL_SIZE)
    
    # ** NEW LOGIC: Create a diverse pool of unique foundations **
    foundation_candidates = {}
    for score, idx in zip(retrieval_hits[0][0], retrieval_hits[1][0]):
        grant_id = grant_ids[idx]
        cursor.execute("SELECT id, enriched_purpose, foundation_ein FROM grants WHERE id = ?", (grant_id,))
        grant_data = cursor.fetchone()
        
        if grant_data:
            ein = grant_data['foundation_ein']
            if ein not in eins_to_exclude:
                if ein not in foundation_candidates or score > foundation_candidates[ein]['retrieval_score']:
                    foundation_candidates[ein] = {
                        'text': grant_data['enriched_purpose'],
                        'retrieval_score': score.item()
                    }
    
    candidate_list_for_reranking = [{'foundation_ein': ein, 'text': data['text']} for ein, data in foundation_candidates.items()]

    if not candidate_list_for_reranking:
        print("No new candidates found to rank.")
    else:
        print(f"--- Stage 2: Re-ranking {len(candidate_list_for_reranking)} unique foundations for accuracy ---")
        reranker_input_pairs = [[charity_profile, candidate['text'] or ""] for candidate in candidate_list_for_reranking]
        reranker_scores_raw = reranker.predict(reranker_input_pairs, show_progress_bar=True)
        reranker_scores_prob = torch.sigmoid(torch.tensor(reranker_scores_raw))

        for i, candidate in enumerate(candidate_list_for_reranking):
            ai_similarity_score = reranker_scores_prob[i].item() * 100
            
            cursor.execute("SELECT city, state FROM foundations WHERE ein = ?", (candidate['foundation_ein'],))
            location_data = cursor.fetchone()
            geo_score = 0
            if location_data:
                if (location_data['state'] or '').upper() == charity_state: geo_score = 75.0
                if (location_data['city'] or '').upper() == charity_city: geo_score = 100.0
            
            asset_score = calculate_asset_score(cursor, candidate['foundation_ein'])
            growth_score = calculate_giving_growth_score(cursor, candidate['foundation_ein'])
            national_giver_score = 100.0 if is_national_giver(cursor, candidate['foundation_ein']) else 0.0
            
            candidate['final_score'] = (ai_similarity_score * WEIGHT_AI_SIMILARITY) + \
                                      (asset_score * WEIGHT_ASSET_SIZE) + \
                                      (growth_score * WEIGHT_GIVING_GROWTH) + \
                                      (geo_score * WEIGHT_GEOGRAPHY) + \
                                      (national_giver_score * WEIGHT_NATIONAL_GIVER)

        candidate_list_for_reranking.sort(key=lambda x: x['final_score'], reverse=True)
    
    # --- 4. Assemble and Display Final List ---
    final_ranked_list = []
    for ein in past_funder_eins:
        if ein not in reviewed_eins:
             final_ranked_list.append({'ein': ein, 'score': 100.0, 'note': '(Past Funder)'})

    for candidate in candidate_list_for_reranking:
        if len(final_ranked_list) >= TOP_N_MATCHES: break
        if not any(f['ein'] == candidate['foundation_ein'] for f in final_ranked_list):
            final_ranked_list.append({'ein': candidate['foundation_ein'], 'score': candidate['final_score'], 'note': ''})

    print("\n--- Top Foundation Matches Found ---")
    print(f"{'Rank':<5}{'Foundation Name':<50}{'EIN':<15}{'Note'}")
    print("-" * 80)

    final_ranked_list.sort(key=lambda x: x['score'], reverse=True)

    displayed_matches = []
    for i, foundation in enumerate(final_ranked_list):
        cursor.execute("SELECT name FROM foundations WHERE ein = ?", (foundation['ein'],))
        foundation_data = cursor.fetchone()
        if foundation_data:
            print(f"{i+1:<5}{(foundation_data['name'] or 'N/A')[:48]:<50}{foundation['ein']:<15}{foundation['note']}")
            displayed_matches.append({'rank': i + 1, 'ein': foundation['ein']})

    # --- 5. Ask for user feedback ---
    print("\n--- Feedback Loop ---")
    print("Enter the rank number of a foundation to 'Like' (add to pipeline) or 'Dislike' (hide from future results).")
    liked_input = input("Which ranks to LIKE? (Press Enter to skip): ")
    disliked_input = input("Which ranks to DISLIKE? (Press Enter to skip): ")

    feedback_to_save = []
    if liked_input:
        ranks = [int(r.strip()) for r in liked_input.split(',') if r.strip().isdigit()]
        for r in ranks:
            match = next((m for m in displayed_matches if m['rank'] == r), None)
            if match: feedback_to_save.append((match['ein'], 'liked'))
    
    if disliked_input:
        ranks = [int(r.strip()) for r in disliked_input.split(',') if r.strip().isdigit()]
        for r in ranks:
            match = next((m for m in displayed_matches if m['rank'] == r), None)
            if match: feedback_to_save.append((match['ein'], 'disliked'))

    if feedback_to_save:
        save_feedback(cursor, feedback_to_save)
        conn.commit()

    conn.close()
    print("\n--- Matchmaking complete. ---")

if __name__ == "__main__":
    find_top_matches()
