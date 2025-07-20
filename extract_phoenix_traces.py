#!/usr/bin/env python3
"""
Extract Phoenix traces for a specific project and save to files.
"""

import os
from datetime import datetime
import phoenix as px
from dotenv import load_dotenv

def main():
    # Load environment variables
    load_dotenv()
    
    # Get the service name from environment
    service_name = os.getenv("OTEL_SERVICE_NAME", "default")
    print(f"Extracting traces for project: {service_name}")
    
    # Connect to Phoenix (default is localhost:6006)
    phoenix_endpoint = os.getenv("PHOENIX_ENDPOINT", "http://localhost:6006")
    client = px.Client(endpoint=phoenix_endpoint)
    
    try:
        # Extract all traces/spans for the project
        print("Fetching traces from Phoenix...")
        df = client.get_spans_dataframe(project_name=service_name)
        
        if df.empty:
            print(f"No traces found for project: {service_name}")
            return
        
        print(f"Found {len(df)} spans in the traces")
        
        # Create output directory
        output_dir = "trace_exports"
        os.makedirs(output_dir, exist_ok=True)
        
        # Create timestamp for file naming
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        # Save to CSV format
        csv_file = os.path.join(output_dir, f"{service_name}_traces_{timestamp}.csv")
        df.to_csv(csv_file, index=True)
        print(f"Saved traces to: {csv_file}")
        
        # Print summary info
        print("\nTrace Summary:")
        print(f"Total spans: {len(df)}")
        if 'span_kind' in df.columns:
            print(f"Span kinds: {df['span_kind'].value_counts().to_dict()}")
        if 'start_time' in df.columns:
            print(f"Time range: {df['start_time'].min()} to {df['start_time'].max()}")
        
    except Exception as e:
        print(f"Error extracting traces: {e}")
        print("Make sure Phoenix is running at http://localhost:6006")

if __name__ == "__main__":
    main()