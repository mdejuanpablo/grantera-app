import sqlite3
import json
import os
from sentence_transformers import SentenceTransformer
import numpy as np

# --- CONFIGURATION ---
DB_FILE = "grant_matches.db"
OUTPUT_FILE = "grant_embeddings.json"
# This is the name of a popular, high-quality model for creating sentence embeddings.
# The library will automatically download it the first time you run the script.
MODEL_NAME = 'all-MiniLM-L6-v2'

def generate_embeddings():
    """
    Reads grant purposes from the database, converts them to vector embeddings
    using a pre-trained model, and saves them to a file.
    """
    if not os.path.exists(DB_FILE):
        print(f"Error: Database file '{DB_FILE}' not found.")
        print("Please run the 'local_parser.py' script first to create the database.")
        return

    print("--- Starting Embedding Generation ---")
    
    # 1. Load the pre-trained Sentence Transformer model
    print(f"Loading AI model: '{MODEL_NAME}'...")
    # This may take a few minutes the first time it runs as it downloads the model.
    model = SentenceTransformer(MODEL_NAME)
    print("Model loaded successfully.")

    # 2. Connect to the database and fetch all grant purposes
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    print("Fetching grant data from the database...")
    # We select the grant ID and the enriched purpose to process
    cursor.execute("SELECT id, enriched_purpose FROM grants WHERE enriched_purpose IS NOT NULL")
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        print("No grants with an 'enriched_purpose' found in the database to process.")
        return

    print(f"Found {len(rows)} grants to process.")

    # 3. Create embeddings for each grant purpose
    print("Generating embeddings... (This may take some time depending on the number of grants)")
    
    # Get just the text descriptions from our database rows
    grant_texts = [row['enriched_purpose'] for row in rows]
    
    # The model's 'encode' method efficiently converts all texts to vectors in a batch
    embeddings = model.encode(grant_texts, show_progress_bar=True)
    
    print("Embedding generation complete.")

    # 4. Save the results to a file
    # We store the ID of each grant alongside its vector embedding
    output_data = {
        "grant_ids": [row['id'] for row in rows],
        # We convert the numpy array to a standard Python list for JSON compatibility
        "embeddings": embeddings.tolist() 
    }

    with open(OUTPUT_FILE, 'w') as f:
        json.dump(output_data, f)

    print(f"\n--- Success! Saved {len(rows)} embeddings to '{OUTPUT_FILE}' ---")


if __name__ == "__main__":
    generate_embeddings()