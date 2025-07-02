import re
import sqlite3
import xml.etree.ElementTree as ET
import os
import glob
import csv
import multiprocessing
from functools import partial
from typing import Dict, List, Optional

from thefuzz import fuzz

# --- CONSTANTS AND CONFIGURATION ---
DB_FILE = "grant_matches.db"
XML_FOLDER = "xml_files_to_process"
MASTER_CHARITIES_CSV = "master_charities.csv"
GOOD_NAME_SIMILARITY = 85
AGGRESSIVE_TERMS_TO_STRIP = {
    'school', 'university', 'college', 'church', 'center', 'society', 
    'association', 'club', 'league', 'inc', 'of', 'the', 'and', 'for',
    'foundation', 'fund', 'trust', 'charity', 'program', 'department'
}
EIN_ALIASES = ['EIN', 'EMPLOYER IDENTIFICATION NUMBER']
NAME_ALIASES = ['NAME', 'PRIMARY_NAME', 'ORGANIZATION_NAME']

master_db_global = None

def normalize_name(name: str) -> str:
    if not name: return ""
    s = name.lower().replace('&', 'and')
    s = re.sub(r'[^\w\s]', '', s)
    return " ".join(s.split())

def aggressive_normalize_name(name: str) -> str:
    s = normalize_name(name)
    return " ".join([token for token in s.split() if token not in AGGRESSIVE_TERMS_TO_STRIP])

def find_best_match(grant: Dict, master_db: List[Dict]) -> Optional[str]:
    original_name = grant.get('recipient_name', '')
    if not original_name: return None
    normalized_grant_name = aggressive_normalize_name(original_name)
    if not normalized_grant_name: return None
    best_match_ein = None
    highest_score = 0
    for candidate in master_db:
        score = fuzz.token_set_ratio(normalized_grant_name, candidate['normalized_name_agg'])
        if score > highest_score:
            highest_score = score
            best_match_ein = candidate['ein']
    if highest_score >= GOOD_NAME_SIMILARITY:
        return best_match_ein
    return None

# --- DATABASE AND FILE FUNCTIONS ---
def setup_database():
    """Creates the database tables, now including a charity_profiles table."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    # Existing tables
    cursor.execute("CREATE TABLE IF NOT EXISTS foundations (ein TEXT PRIMARY KEY, name TEXT, address_line_1 TEXT, city TEXT, state TEXT, zip_code TEXT, formation_year INTEGER, end_of_year_assets REAL, mission_statement TEXT)")
    cursor.execute("CREATE TABLE IF NOT EXISTS officers (id INTEGER PRIMARY KEY AUTOINCREMENT, foundation_ein TEXT, name TEXT, title TEXT, UNIQUE(foundation_ein, name, title))")
    cursor.execute("CREATE TABLE IF NOT EXISTS grants (id INTEGER PRIMARY KEY AUTOINCREMENT, foundation_ein TEXT, recipient_name TEXT, recipient_city TEXT, recipient_state TEXT, recipient_ein_matched TEXT, grant_amount REAL, grant_purpose TEXT, enriched_purpose TEXT, UNIQUE(foundation_ein, recipient_name, grant_amount, grant_purpose))")
    cursor.execute("CREATE TABLE IF NOT EXISTS feedback (foundation_ein TEXT PRIMARY KEY, status TEXT NOT NULL)")
    
    # === NEW CHARITY PROFILES TABLE ===
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS charity_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            charity_name TEXT NOT NULL,
            charity_ein TEXT,
            charity_phone TEXT,
            address_1 TEXT,
            country TEXT,
            state TEXT,
            city TEXT,
            zip_code TEXT,
            contact_name TEXT,
            contact_title TEXT,
            contact_phone TEXT,
            mission_statement TEXT,
            program_description TEXT,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    """)
    # =================================

    conn.commit()
    conn.close()
    print(f"Database '{DB_FILE}' is set up with new schema, including charity_profiles table.")

# ... (rest of the parser script remains the same) ...

def save_foundation_data(db_conn, foundation_info: Dict):
    sql = "INSERT OR REPLACE INTO foundations (ein, name, address_line_1, city, state, zip_code, formation_year, end_of_year_assets, mission_statement) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);"
    try:
        cur = db_conn.cursor()
        cur.execute(sql, (
            foundation_info.get('ein'), foundation_info.get('name'),
            foundation_info.get('address_line_1'), foundation_info.get('city'),
            foundation_info.get('state'), foundation_info.get('zip_code'),
            foundation_info.get('formation_year'), foundation_info.get('assets'),
            foundation_info.get('mission')
        ))
        db_conn.commit()
    except Exception: pass

def save_officers_data(db_conn, foundation_ein: str, officers: List[Dict]):
    sql = "INSERT OR IGNORE INTO officers (foundation_ein, name, title) VALUES (?, ?, ?);"
    try:
        cur = db_conn.cursor()
        cur.executemany(sql, [(foundation_ein, o['name'], o['title']) for o in officers])
        db_conn.commit()
    except Exception: pass

def save_grant_data(db_conn, foundation_ein: str, grant: Dict, matched_ein: Optional[str]):
    sql = "INSERT OR IGNORE INTO grants (foundation_ein, recipient_name, recipient_city, recipient_state, recipient_ein_matched, grant_amount, grant_purpose, enriched_purpose) VALUES (?, ?, ?, ?, ?, ?, ?, ?);"
    try:
        cur = db_conn.cursor()
        cur.execute(sql, (
            foundation_ein, grant.get('recipient_name'), grant.get('city'), grant.get('state'),
            matched_ein, grant.get('amount'), grant.get('purpose'), grant.get('enriched_purpose')
        ))
        db_conn.commit()
    except Exception: pass

def parse_990pf_xml(file_path: str) -> Optional[Dict]:
    try:
        tree = ET.parse(file_path)
        root = tree.getroot()
        namespace = f"{{{root.tag.split('}')[0][1:]}}}" if '}' in root.tag else ''
        def get_text(node, path):
            found_node = node.find(path)
            return ' '.join(found_node.itertext()).strip() if found_node is not None and found_node.text else None
        def get_numeric(node, path):
            text_val = get_text(node, path)
            return re.sub(r'[$,]', '', text_val) if text_val else None
        
        foundation_info = {"ein": get_text(root, f".//{namespace}Filer/{namespace}EIN"),"name": get_text(root, f".//{namespace}Filer/{namespace}BusinessName/{namespace}BusinessNameLine1Txt"), "formation_year": get_text(root, f".//{namespace}ReturnHeader/{namespace}TaxYr"),"assets": get_numeric(root, f".//{namespace}FMVAssetsEOYAmt") or get_numeric(root, f".//{namespace}TotalAssetsEOYAmt")}
        officers = [{"name": (n.text or '').strip(), "title": (t.text or '').strip()} for node in root.findall(f".//{namespace}OfficerDirTrstKeyEmplInfoGrp") if (n := node.find(f".//{namespace}PersonNm")) is not None and (t := node.find(f".//{namespace}TitleTxt")) is not None]
        grants = [{"recipient_name": name_text.strip(), "amount": get_numeric(node, f"{namespace}Amt"), "purpose": get_text(node, f"{namespace}GrantOrContributionPurposeTxt")} for node in root.findall(f".//{namespace}GrantOrContributionPdDurYrGrp") if (name_text := get_text(node, f"{namespace}RecipientBusinessName/{namespace}BusinessNameLine1Txt") or get_text(node, f"{namespace}RecipientPersonNm")) and get_numeric(node, f"{namespace}Amt")]
        
        return {"foundation_info": foundation_info, "officers": officers, "grants": grants}
    except Exception: return None

def find_column_index(header: list, aliases: list) -> int:
    header_upper = [h.upper() for h in header]
    for alias in aliases:
        try: return header_upper.index(alias.upper())
        except ValueError: continue
    return -1

def load_master_charities_for_worker(filepath: str) -> List[Dict]:
    charities = []
    if not os.path.exists(filepath): return []
    try:
        with open(filepath, mode='r', encoding='utf-8-sig', errors='ignore') as csvfile:
            reader = csv.reader(csvfile)
            header = next(reader, None)
            if not header: return []
            ein_idx = find_column_index(header, EIN_ALIASES)
            name_idx = find_column_index(header, NAME_ALIASES)
            if ein_idx == -1 or name_idx == -1: return []
            for row in reader:
                if len(row) > max(ein_idx, name_idx):
                    ein, name = row[ein_idx], row[name_idx]
                    if ein and name and ein.upper() != 'EIN':
                        charities.append({"ein": ein, "name": name, "normalized_name_agg": aggressive_normalize_name(name)})
        return charities
    except Exception: return []

def init_worker():
    global master_db_global
    master_db_global = load_master_charities_for_worker(MASTER_CHARITIES_CSV)

def process_single_file(file_path: str):
    global master_db_global
    if not master_db_global: return

    parsed_data = parse_990pf_xml(file_path)
    if parsed_data and parsed_data.get('foundation_info', {}).get('ein'):
        conn = sqlite3.connect(DB_FILE, timeout=10)
        foundation_ein = parsed_data['foundation_info']['ein']
        save_foundation_data(conn, parsed_data['foundation_info'])
        save_officers_data(conn, foundation_ein, parsed_data['officers'])
        for grant in parsed_data['grants']:
            matched_ein = find_best_match(grant, master_db_global)
            save_grant_data(conn, foundation_ein, grant, matched_ein)
        conn.close()
        print(".", end='', flush=True)

if __name__ == "__main__":
    print("--- Starting Local Parser ---")
    if os.path.exists(DB_FILE):
        print("Existing database found. Updating schema...")
        setup_database()
        print("Schema updated. To re-parse all data, please delete the 'grant_matches.db' file first.")
    else:
        setup_database()

    search_path = os.path.join(XML_FOLDER, "*.xml")
    files_to_process = glob.glob(search_path)
    
    num_processes = multiprocessing.cpu_count()
    
    if not files_to_process:
        print(f"Error: No XML files found in folder '{XML_FOLDER}'.")
    else:
        print(f"Found {len(files_to_process)} files. Starting {num_processes} worker processes...")
        with multiprocessing.Pool(processes=num_processes, initializer=init_worker) as pool:
            pool.map(process_single_file, files_to_process)
            
        print("\n\n--- All parallel workers finished. ---")
    
    print("--- Parser run complete. ---")
