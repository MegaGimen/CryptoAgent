import os
import json
import re

# --- CONFIGURATION ---
LOG_DIR = "/home/coinautomation/logs"
TARGET_FILE = "output.json"
OUTPUT_REPORT = "errors.json"
SEARCH_TERM = "error"

def extract_errors_from_json(obj, search_term):
    """
    Recursively search for search_term in any string value of the dictionary/list.
    Returns a list of all matching string values.
    """
    matches = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            matches.extend(extract_errors_from_json(value, search_term))
    elif isinstance(obj, list):
        for item in obj:
            matches.extend(extract_errors_from_json(item, search_term))
    elif isinstance(obj, str):
        if search_term in obj.lower():
            matches.append(obj)
    return matches

def process_error_logs(log_dir):
    """
    Search for 'error' in output.json files and extract matching field values.
    Returns a list of dictionaries with folder and error details.
    """
    if not os.path.exists(log_dir):
        print(f"Error: Log directory {log_dir} not found.")
        return []

    all_entries = os.listdir(log_dir)
    folders = [f for f in all_entries if os.path.isdir(os.path.join(log_dir, f)) and re.match(r'^\d{8}_\d{6}$', f)]
    folders.sort()

    all_error_entries = []

    for folder in folders:
        folder_path = os.path.join(log_dir, folder)
        file_path = os.path.join(folder_path, TARGET_FILE)

        if os.path.exists(file_path):
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    # Use the recursive extractor to find all strings containing 'error'
                    found_errors = extract_errors_from_json(data, SEARCH_TERM)
                    
                    if found_errors:
                        all_error_entries.append({
                            "folder": folder,
                            "timestamp": folder, # YYYYMMDD_HHMMSS
                            "errors": found_errors
                        })
            except Exception as e:
                print(f"Warning: Could not process {file_path}: {e}")

    return all_error_entries

if __name__ == "__main__":
    print(f"Searching for values containing '{SEARCH_TERM}' in {LOG_DIR}...")
    error_data = process_error_logs(LOG_DIR)

    if error_data:
        # Export to errors.json
        try:
            with open(OUTPUT_REPORT, 'w', encoding='utf-8') as f:
                json.dump(error_data, f, indent=4, ensure_ascii=False)
            print(f"\n✅ Successfully exported {len(error_data)} log entries to {OUTPUT_REPORT}")
            
            # Print a quick summary to console
            for entry in error_data:
                print(f"[{entry['folder']}] Found {len(entry['errors'])} error field(s).")
        except Exception as e:
            print(f"Error writing to {OUTPUT_REPORT}: {e}")
    else:
        print(f"\nNo values found containing the string '{SEARCH_TERM}'.")
