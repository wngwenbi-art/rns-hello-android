print("=== rns_worker_minimal.py LOADED SUCCESSFULLY ===")

def start(bt_socket_wrapper):
    print(">>> start() function was called from Kotlin!")
    return "TEST1234567890abcdef1234567890abcdef"

def send_image(dest_hash_hex, webp_b64):
    print(f">>> send_image called for {dest_hash_hex[:16]}...")
    return f"Image queued (minimal version, {len(webp_b64)} chars)"

def get_messages():
    return []

def get_announces():
    return []

def announce():
    return "Announced from minimal rns_worker"

def get_address():
    return "TEST1234567890abcdef1234567890abcdef"

print("=== rns_worker_minimal.py finished loading ===")