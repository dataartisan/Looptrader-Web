#!/usr/bin/env python3
"""
Migration script to add entry_value and high_water_mark columns to TrailingStopState table
"""

import os
import sys
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Get database URL
DATABASE_URL = os.getenv('DATABASE_URL')
if not DATABASE_URL:
    db_user = os.getenv('DB_USER', 'admin')
    db_password = os.getenv('DB_PASSWORD', '')
    db_host = os.getenv('DB_HOST', 'localhost')
    db_port = os.getenv('DB_PORT', '5432')
    db_name = os.getenv('DB_NAME', 'looptrader')
    DATABASE_URL = f"postgresql://{db_user}:{db_password}@{db_host}:{db_port}/{db_name}"

print(f"Connecting to database...")

try:
    # Create engine
    engine = create_engine(DATABASE_URL)
    
    with engine.connect() as conn:
        # Start transaction
        trans = conn.begin()
        
        try:
            # Check if columns already exist
            check_sql = text("""
                SELECT column_name 
                FROM information_schema.columns 
                WHERE table_name = 'TrailingStopState' 
                AND column_name IN ('entry_value', 'high_water_mark', 'created_at', 'updated_at')
            """)
            
            result = conn.execute(check_sql)
            existing_columns = [row[0] for row in result]
            
            print(f"Existing columns: {existing_columns}")
            
            # Add entry_value column if it doesn't exist
            if 'entry_value' not in existing_columns:
                print("Adding entry_value column...")
                conn.execute(text("""
                    ALTER TABLE "TrailingStopState" 
                    ADD COLUMN entry_value DOUBLE PRECISION
                """))
                print("✓ entry_value column added")
            else:
                print("✓ entry_value column already exists")
            
            # Add high_water_mark column if it doesn't exist
            if 'high_water_mark' not in existing_columns:
                print("Adding high_water_mark column...")
                conn.execute(text("""
                    ALTER TABLE "TrailingStopState" 
                    ADD COLUMN high_water_mark DOUBLE PRECISION
                """))
                print("✓ high_water_mark column added")
            else:
                print("✓ high_water_mark column already exists")
            
            # Add created_at column if it doesn't exist
            if 'created_at' not in existing_columns:
                print("Adding created_at column...")
                conn.execute(text("""
                    ALTER TABLE "TrailingStopState" 
                    ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                """))
                print("✓ created_at column added")
            else:
                print("✓ created_at column already exists")
            
            # Add updated_at column if it doesn't exist
            if 'updated_at' not in existing_columns:
                print("Adding updated_at column...")
                conn.execute(text("""
                    ALTER TABLE "TrailingStopState" 
                    ADD COLUMN updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                """))
                print("✓ updated_at column added")
            else:
                print("✓ updated_at column already exists")
            
            # Commit transaction
            trans.commit()
            print("\n✅ Migration completed successfully!")
            
            # Show final table structure
            print("\nFinal table structure:")
            result = conn.execute(text("""
                SELECT column_name, data_type, is_nullable 
                FROM information_schema.columns 
                WHERE table_name = 'TrailingStopState' 
                ORDER BY ordinal_position
            """))
            
            for row in result:
                nullable = "NULL" if row[2] == 'YES' else "NOT NULL"
                print(f"  {row[0]:<25} {row[1]:<20} {nullable}")
            
        except Exception as e:
            trans.rollback()
            print(f"\n❌ Error during migration: {e}")
            sys.exit(1)
            
except Exception as e:
    print(f"\n❌ Error connecting to database: {e}")
    sys.exit(1)
