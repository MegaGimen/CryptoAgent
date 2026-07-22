import sys
import os

# Add current directory to path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from main import protocol

if __name__ == "__main__":
    # The command requested by the user
    command = "borrow ETH 0.05 None FALSE"
    print(f"🚀 Executing: {command}")
    results = protocol(command)
    print("\n📊 Results:")
    for res in results:
        print(res)
