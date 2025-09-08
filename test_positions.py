#!/usr/bin/env python3
"""
Quick test script to check positions data directly
"""
import sys
import os
sys.path.append('/app/src/looptrader_web')

from models.database import SessionLocal, Position, get_recent_positions

def main():
    print("Testing positions data...")
    
    # Test direct database access
    db = SessionLocal()
    try:
        all_positions = db.query(Position).all()
        print(f"Total positions in database: {len(all_positions)}")
        
        if all_positions:
            print("\nFirst few positions:")
            for i, pos in enumerate(all_positions[:3]):
                print(f"  {i+1}. ID: {pos.id}, Active: {pos.active}, Opened: {pos.opened_datetime}")
        
        # Test recent positions function
        recent_positions = get_recent_positions(5)
        print(f"\nRecent positions count: {len(recent_positions)}")
        
        # Test positions page query
        query = db.query(Position).order_by(Position.opened_datetime.desc())
        positions_page = query.all()
        print(f"Positions page query count: {len(positions_page)}")
        
    except Exception as e:
        print(f"Error: {e}")
    finally:
        db.close()

if __name__ == "__main__":
    main()
