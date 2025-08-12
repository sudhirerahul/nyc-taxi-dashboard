from flask import Flask, jsonify, render_template_string, request, redirect, url_for
from flask_cors import CORS
import os
import json
from datetime import datetime, timedelta
import traceback
import sqlite3
import pandas as pd
from werkzeug.utils import secure_filename
import zipfile
import tempfile
import shutil
import os

# Optional geopandas import for shapefile support
try:
    import geopandas as gpd
    GEOPANDAS_AVAILABLE = True
    print("✅ GeoPandas available - shapefile upload enabled")
except ImportError:
    GEOPANDAS_AVAILABLE = False
    print("⚠️  GeoPandas not available - shapefile upload disabled")

app = Flask(__name__)
CORS(app)

# Configuration
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB max file size
UPLOAD_FOLDER = 'uploads'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# Ensure upload folder exists
UPLOAD_FOLDER = os.environ.get('UPLOAD_FOLDER', '/tmp/uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# Your actual data paths
FHVHV_PATH = "/Users/sudhirerahul/Desktop/Python/fhvhv_2023_2024"
TAXI_ZONES_PATH = "/Users/sudhirerahul/Desktop/Python/taxi_zones"

# Global database connection
db_path = os.environ.get("DB_PATH", "/data/taxi_data.db")
zones_geojson_path = "taxi_zones.geojson"

def init_database():
    """Initialize SQLite database and load the actual data using pandas"""
    try:
        # Check if database already exists with data
        if os.path.exists(db_path):
            try:
                conn = get_db_connection()
                cursor = conn.cursor()
                # Check if we have the total_fare_amount column
                cursor.execute("PRAGMA table_info(fhvhv)")
                columns = [row[1] for row in cursor.fetchall()]
                if 'total_fare_amount' in columns:
                    cursor.execute("SELECT COUNT(*) FROM fhvhv")
                    count = cursor.fetchone()[0]
                    conn.close()
                    
                    if count > 0:
                        print(f"Database already exists with {count:,} records and total_fare_amount column!")
                        print("Skipping data loading, using existing database...")
                        return True
                else:
                    print("Database exists but missing total_fare_amount column, recreating...")
                    conn.close()
                    os.remove(db_path)
            except:
                print("Existing database corrupted, recreating...")
                os.remove(db_path)
        
        print("Loading FHVHV data from:", FHVHV_PATH)
        
        # Find ONLY 2024 parquet files
        parquet_files = []
        for file in os.listdir(FHVHV_PATH):
            if file.endswith('.parquet') and '2024' in file:
                parquet_files.append(os.path.join(FHVHV_PATH, file))
        
        if not parquet_files:
            print("No 2024 parquet files found!")
            print("Available files:")
            for file in os.listdir(FHVHV_PATH):
                if file.endswith('.parquet'):
                    print(f"  {file}")
            return False
        
        print(f"Found {len(parquet_files)} 2024 parquet files:")
        for file in parquet_files:
            print(f"  {os.path.basename(file)}")
        
        # Create SQLite connection
        conn = sqlite3.connect(db_path)
        
        # Create table with optimized schema INCLUDING total_fare_amount
        conn.execute('''
            CREATE TABLE IF NOT EXISTS fhvhv (
                PULocationID INTEGER,
                DOLocationID INTEGER,
                pickup_datetime TEXT,
                pickup_hour INTEGER,
                day_of_week INTEGER,
                pickup_month INTEGER,
                day_type TEXT,
                trip_miles REAL,
                duration_minutes REAL,
                price_per_mile REAL,
                total_fare_amount REAL,
                wait_time_minutes REAL
            )
        ''')
        
        total_records = 0
        for i, file_path in enumerate(parquet_files):
            print(f"Processing file {i+1}/{len(parquet_files)}: {os.path.basename(file_path)}")
            
            try:
                # Read parquet file in chunks to avoid memory issues
                print(f"  Reading {os.path.basename(file_path)}...")
                df = pd.read_parquet(file_path)
                
                print(f"  Loaded {len(df):,} raw records")
                
                # Basic data cleaning
                print("  Cleaning data...")
                df = df.dropna(subset=['pickup_datetime', 'dropoff_datetime', 'PULocationID', 'DOLocationID'])
                df = df[(df['trip_miles'] > 0) & (df['trip_time'] > 0)]
                
                print(f"  After cleaning: {len(df):,} records")
                
                if len(df) == 0:
                    print("  No valid records after cleaning, skipping...")
                    continue
                
                # Convert datetime and filter for 2024 (double check)
                df['pickup_datetime'] = pd.to_datetime(df['pickup_datetime'])
                df = df[df['pickup_datetime'].dt.year == 2024]
                
                print(f"  After 2024 filter: {len(df):,} records")
                
                if len(df) == 0:
                    print("  No 2024 records found, skipping...")
                    continue
                
                # Add calculated fields efficiently
                print("  Adding calculated fields...")
                df['pickup_hour'] = df['pickup_datetime'].dt.hour
                df['day_of_week'] = df['pickup_datetime'].dt.dayofweek
                df['pickup_month'] = df['pickup_datetime'].dt.month
                
                # Calculate day type
                df['day_type'] = df['day_of_week'].apply(lambda x: 'weekend' if x in [5, 6] else 'weekday')
                
                # Calculate total fare amount - THIS IS THE KEY ADDITION
                total_fare = (
                    df['base_passenger_fare'].fillna(0) + 
                    df['tolls'].fillna(0) + 
                    df['bcf'].fillna(0) + 
                    df['sales_tax'].fillna(0) + 
                    df['congestion_surcharge'].fillna(0) + 
                    df['airport_fee'].fillna(0)
                )
                df['total_fare_amount'] = total_fare
                
                # Calculate price per mile safely
                df['price_per_mile'] = total_fare / df['trip_miles']
                
                # Calculate duration in minutes
                df['duration_minutes'] = df['trip_time'] / 60.0
                
                # Calculate wait time in minutes (simplified)
                if 'on_scene_datetime' in df.columns and 'request_datetime' in df.columns:
                    try:
                        df['request_datetime'] = pd.to_datetime(df['request_datetime'])
                        df['on_scene_datetime'] = pd.to_datetime(df['on_scene_datetime'])
                        wait_time = (df['on_scene_datetime'] - df['request_datetime']).dt.total_seconds() / 60.0
                        df['wait_time_minutes'] = wait_time.fillna(0).clip(0, 60)  # Cap at 60 minutes
                    except:
                        df['wait_time_minutes'] = 0
                else:
                    df['wait_time_minutes'] = 0
                
                # Select essential columns INCLUDING total_fare_amount
                columns_to_keep = [
                    'PULocationID', 'DOLocationID', 'pickup_datetime',
                    'pickup_hour', 'day_of_week', 'pickup_month', 'day_type',
                    'trip_miles', 'duration_minutes', 'price_per_mile', 'total_fare_amount', 'wait_time_minutes'
                ]
                
                df_clean = df[columns_to_keep].copy()
                
                # Filter out extreme outliers to save space and improve quality
                df_clean = df_clean[
                    (df_clean['price_per_mile'] > 0) & 
                    (df_clean['price_per_mile'] < 50) &  # Remove extreme prices
                    (df_clean['total_fare_amount'] > 0) & 
                    (df_clean['total_fare_amount'] < 500) &  # Remove extreme total fares
                    (df_clean['duration_minutes'] > 1) & 
                    (df_clean['duration_minutes'] < 300) &  # Remove extreme durations
                    (df_clean['trip_miles'] < 100)  # Remove extreme distances
                ]
                
                print(f"  After outlier removal: {len(df_clean):,} records")
                
                # Write to SQLite in smaller batches
                print("  Writing to database...")
                df_clean.to_sql('fhvhv', conn, if_exists='append', index=False, method='multi', chunksize=10000)
                
                records_added = len(df_clean)
                total_records += records_added
                print(f"  Added {records_added:,} records from {os.path.basename(file_path)}")
                
                # Free memory
                del df, df_clean
                
            except Exception as e:
                print(f"  Error processing {file_path}: {str(e)}")
                continue
        
        # Create indexes for better performance
        print("Creating database indexes...")
        cursor = conn.cursor()
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_pu_do ON fhvhv(PULocationID, DOLocationID)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_pickup_hour ON fhvhv(pickup_hour)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_day_of_week ON fhvhv(day_of_week)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_pickup_month ON fhvhv(pickup_month)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_day_type ON fhvhv(day_type)")
        
        conn.commit()
        conn.close()
        
        if total_records == 0:
            print("No records were loaded! Check your parquet files.")
            return False
        
        print(f"Successfully loaded {total_records:,} total records for 2024")
        print(f"Database ready for analysis with total fare amount tracking!")
        return True
        
    except Exception as e:
        print(f"Database initialization error: {str(e)}")
        print(traceback.format_exc())
        return False

def get_db_connection():
    """Get SQLite database connection"""
    return sqlite3.connect(db_path)

def get_zone_names():
    """Load zone names from uploaded shapefile or return default mapping"""
    zone_names = {}
    
    # Try to load from uploaded shapefile first
    if os.path.exists(zones_geojson_path):
        try:
            with open(zones_geojson_path, 'r') as f:
                geojson_data = json.load(f)
                
            for feature in geojson_data.get('features', []):
                props = feature.get('properties', {})
                zone_id = props.get('LocationID') or props.get('OBJECTID') or props.get('zone_id') or props.get('id') or props.get('Zone')
                zone_name = props.get('zone') or props.get('Zone') or props.get('borough') or props.get('name') or props.get('zone_name')
                
                if zone_id and zone_name:
                    zone_names[int(zone_id)] = zone_name
                    
        except Exception as e:
            print(f"Error loading zone names from shapefile: {e}")
    
    # If no zones loaded from shapefile, use default NYC taxi zones mapping
    if not zone_names:
        zone_names = {
            1: "Newark Airport", 2: "Jamaica Bay", 3: "Allerton/Pelham Gardens", 4: "Alphabet City",
            5: "Arden Heights", 6: "Arrochar/Fort Wadsworth", 7: "Astoria", 8: "Astoria Park",
            9: "Auburndale", 10: "Baisley Park", 11: "Bath Beach", 12: "Battery Park",
            13: "Battery Park City", 14: "Bay Ridge", 15: "Bay Terrace/Fort Totten", 16: "Bayside",
            17: "Bedford", 18: "Bedford Park", 19: "Bellerose", 20: "Belmont", 21: "Bensonhurst East",
            22: "Bensonhurst West", 23: "Bloomfield/Emerson Hill", 24: "Bloomingdale", 25: "Boerum Hill",
            26: "Borough Park", 27: "Breezy Point/Fort Tilden/Riis Beach", 28: "Brighton Beach",
            29: "Broad Channel", 30: "Bronx Park", 31: "Bronxdale", 32: "Brooklyn Heights",
            33: "Brooklyn Navy Yard", 34: "Brownsville", 35: "Bushwick North", 36: "Bushwick South",
            37: "Cambria Heights", 38: "Canarsie", 39: "Carroll Gardens", 40: "Central Harlem",
            41: "Central Harlem North", 42: "Central Park", 43: "Charleston/Tottenville", 44: "Chinatown",
            45: "City Island", 46: "Claremont/Bathgate", 47: "Clinton East", 48: "Clinton West",
            49: "Co-Op City", 50: "Cobble Hill", 51: "College Point", 52: "Columbia Street",
            53: "Coney Island", 54: "Corona", 55: "Country Club", 56: "Crotona Park",
            57: "Crotona Park East", 58: "Crown Heights North", 59: "Crown Heights South",
            60: "Cypress Hills", 61: "DUMBO/Vinegar Hill", 62: "Douglaston", 63: "Downtown Brooklyn/MetroTech",
            64: "Dyker Heights", 65: "East Chelsea", 66: "East Concourse/Concourse Village",
            67: "East Elmhurst", 68: "East Flatbush/Farragut", 69: "East Flatbush/Remsen Village",
            70: "East Harlem North", 71: "East Harlem South", 72: "East New York",
            73: "East New York/Pennsylvania Avenue", 74: "East Tremont", 75: "East Village",
            76: "East Williamsburg", 77: "Eastchester", 78: "Elmhurst", 79: "Elmhurst/Maspeth",
            80: "Eltingville/Annadale/Prince's Bay", 81: "Far Rockaway", 82: "Financial District North",
            83: "Financial District South", 84: "Flatbush/Ditmas Park", 85: "Flatiron",
            86: "Flatlands", 87: "Flushing", 88: "Flushing Meadows-Corona Park", 89: "Fordham South",
            90: "Forest Hills", 91: "Fort Greene", 92: "Fresh Meadows", 93: "Freshkills Park",
            94: "Garment District", 95: "Glen Oaks", 96: "Glendale", 97: "Governor's Island/Ellis Island/Liberty Island",
            98: "Gowanus", 99: "Gramercy", 100: "Gravesend", 101: "Great Kills", 102: "Greenpoint",
            103: "Greenwich Village North", 104: "Greenwich Village South", 105: "Grymes Hill/Clifton",
            106: "Hamilton Heights", 107: "Hammels/Arverne", 108: "Hampton Bays", 109: "Heartland Village/Todt Hill",
            110: "Highbridge", 111: "Hollis", 112: "Homecrest", 113: "Howard Beach", 114: "Hunts Point",
            115: "Inwood", 116: "Inwood Hill Park", 117: "Jackson Heights", 118: "Jamaica",
            119: "Jamaica Estates", 120: "JFK Airport", 121: "Kensington", 122: "Kew Gardens",
            123: "Kew Gardens Hills", 124: "Kingsbridge Heights", 125: "Kips Bay", 126: "LaGuardia Airport",
            127: "Laurelton", 128: "Lenox Hill East", 129: "Lenox Hill West", 130: "Lincoln Square East",
            131: "Lincoln Square West", 132: "Little Italy/NoLiTa", 133: "Long Island City/Hunters Point",
            134: "Long Island City/Queens Plaza", 135: "Longwood", 136: "Lower East Side",
            137: "Madison", 138: "Manhattanville", 139: "Marble Hill", 140: "Marine Park/Floyd Bennett Field",
            141: "Mariners Harbor", 142: "Maspeth", 143: "Meatpacking/West Village West", 144: "Melrose South",
            145: "Middle Village", 146: "Midtown Center", 147: "Midtown East", 148: "Midtown North",
            149: "Midtown South", 150: "Midwood", 151: "Morningside Heights", 152: "Morrisania/Melrose",
            153: "Mott Haven/Port Morris", 154: "Mount Hope", 155: "Murray Hill", 156: "Murray Hill-Queens",
            157: "New Dorp/Midland Beach", 158: "New Springville/Bloomfield/Travis", 159: "North Corona",
            160: "Norwood", 161: "Oakland Gardens", 162: "Oakwood", 163: "Ocean Hill", 164: "Ocean Parkway South",
            165: "Old Astoria", 166: "Ozone Park", 167: "Park Slope", 168: "Parkchester", 169: "Pelham Bay",
            170: "Pelham Bay Park", 171: "Pelham Parkway", 172: "Penn Station/Madison Sq West", 173: "Port Richmond",
            174: "Prospect-Lefferts Gardens", 175: "Prospect Heights", 176: "Prospect Park", 177: "Queens Village",
            178: "Queensboro Hill", 179: "Queensbridge/Ravenswood", 180: "Randalls Island", 181: "Red Hook",
            182: "Rego Park", 183: "Richmond Hill", 184: "Ridgewood", 185: "Rikers Island",
            186: "Riverdale/North Riverdale/Fieldston", 187: "Rockaway Park", 188: "Roosevelt Island",
            189: "Rosedale", 190: "Rossville/Woodrow", 191: "Saint Albans", 192: "Saint George/New Brighton",
            193: "Saint Michaels Cemetery/Woodside", 194: "Schuylerville/Throgs Neck/Edgewater Park", 195: "Seagate/Coney Island",
            196: "Sheepshead Bay", 197: "SoHo", 198: "Soundview/Bruckner", 199: "Soundview/Castle Hill",
            200: "South Beach/Dongan Hills", 201: "South Jamaica", 202: "South Ozone Park", 203: "South Williamsburg",
            204: "Springfield Gardens North", 205: "Springfield Gardens South", 206: "Spuyten Duyvil/Kingsbridge",
            207: "Stapleton", 208: "Stuy Town/Peter Cooper Village", 209: "Stuyvesant Heights",
            210: "Sunnyside", 211: "Sunset Park East", 212: "Sunset Park West", 213: "Sutton Place/Turtle Bay East",
            214: "Theater District", 215: "Times Sq/Theatre District", 216: "TriBeCa/Civic Center", 217: "Two Bridges/Seward Park",
            218: "Union Sq", 219: "University Heights/Morris Heights", 220: "Upper East Side North",
            221: "Upper East Side South", 222: "Upper West Side North", 223: "Upper West Side South",
            224: "Van Cortlandt Village", 225: "Van Cortlandt Park", 226: "Van Nest/Morris Park", 227: "Washington Heights North",
            228: "Washington Heights South", 229: "West Brighton", 230: "West Chelsea/Hudson Yards", 231: "West Concourse",
            232: "West Farms/Bronx River", 233: "West Village", 234: "Westchester Village/Unionport",
            235: "Westerleigh", 236: "Whitestone", 237: "Willets Point", 238: "Williamsbridge/Olinville",
            239: "Williamsburg (North Side)", 240: "Williamsburg (South Side)", 241: "Windsor Terrace", 242: "Woodhaven",
            243: "Woodlawn/Wakefield", 244: "Woodside", 245: "World Trade Center", 246: "Yorkville East",
            247: "Yorkville West", 248: "Allerton/Pelham Gardens", 249: "Kingsbridge Heights", 250: "Borough Park",
            251: "Canarsie", 252: "Sheepshead Bay", 253: "Park Slope", 254: "Crown Heights South",
            255: "East New York", 256: "Flatbush/Ditmas Park", 257: "Sunset Park West", 258: "Bensonhurst East",
            259: "Bay Ridge", 260: "Red Hook", 261: "Carroll Gardens", 262: "DUMBO/Vinegar Hill",
            263: "Downtown Brooklyn/MetroTech", 264: "Fort Greene", 265: "Boerum Hill"
        }
    
    return zone_names

def process_shapefile(file_path):
    """Process uploaded shapefile and convert to GeoJSON"""
    if not GEOPANDAS_AVAILABLE:
        raise ValueError("GeoPandas not available. Cannot process shapefiles.")
    
    try:
        # Create temporary directory for extraction
        temp_dir = tempfile.mkdtemp()
        
        # Extract the zip file
        with zipfile.ZipFile(file_path, 'r') as zip_ref:
            zip_ref.extractall(temp_dir)
        
        # Find the .shp file
        shp_file = None
        for file in os.listdir(temp_dir):
            if file.endswith('.shp'):
                shp_file = os.path.join(temp_dir, file)
                break
        
        if not shp_file:
            raise ValueError("No .shp file found in the uploaded zip")
        
        # Read the shapefile
        gdf = gpd.read_file(shp_file)
        
        # Ensure we have the right projection (WGS84)
        if gdf.crs is None:
            gdf.crs = 'EPSG:4326'
        elif gdf.crs != 'EPSG:4326':
            gdf = gdf.to_crs('EPSG:4326')
        
        # Convert to GeoJSON
        geojson_data = json.loads(gdf.to_json())
        
        # Save to file
        with open(zones_geojson_path, 'w') as f:
            json.dump(geojson_data, f)
        
        # Clean up temporary directory
        shutil.rmtree(temp_dir)
        
        return geojson_data
        
    except Exception as e:
        # Clean up on error
        if 'temp_dir' in locals():
            shutil.rmtree(temp_dir, ignore_errors=True)
        raise e

@app.route('/')
def index():
    """Serve the main dashboard"""
    return render_template_string(DASHBOARD_HTML)

@app.route('/upload-zones', methods=['POST'])
def upload_zones():
    """Handle shapefile upload"""
    try:
        if not GEOPANDAS_AVAILABLE:
            return jsonify({
                "error": "Shapefile upload not available. GeoPandas is not installed. Please install it with: pip install geopandas"
            }), 500
        
        if 'shapefile' not in request.files:
            return jsonify({"error": "No file uploaded"}), 400
        
        file = request.files['shapefile']
        if file.filename == '':
            return jsonify({"error": "No file selected"}), 400
        
        if file and file.filename.lower().endswith('.zip'):
            filename = secure_filename(file.filename)
            file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(file_path)
            
            # Process the shapefile
            geojson_data = process_shapefile(file_path)
            
            # Clean up uploaded file
            os.remove(file_path)
            
            return jsonify({
                "message": "Shapefile processed successfully",
                "zones_count": len(geojson_data['features'])
            })
        else:
            return jsonify({"error": "Please upload a ZIP file containing the shapefile"}), 400
            
    except Exception as e:
        print(f"Upload error: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/taxi-zones')
def get_taxi_zones():
    """Get taxi zones from uploaded shapefile or default"""
    try:
        if os.path.exists(zones_geojson_path):
            with open(zones_geojson_path, 'r') as f:
                return jsonify(json.load(f))
        else:
            # Return empty GeoJSON if no zones uploaded
            return jsonify({
                "type": "FeatureCollection",
                "features": []
            })
    except Exception as e:
        print(f"Error loading zones: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/route-analysis')
def analyze_route():
    """Analyze route between two zones with REAL data including total fare amount"""
    try:
        pickup_zone = request.args.get('pickup', type=int)
        dropoff_zone = request.args.get('dropoff', type=int)
        day_type = request.args.get('day_type', 'all')
        
        if not pickup_zone or not dropoff_zone:
            return jsonify({"error": "Missing pickup or dropoff zone"}), 400
        
        print(f"Analyzing route: {pickup_zone} -> {dropoff_zone} ({day_type})")
        
        # Build day type filter
        day_filter = ""
        if day_type == 'weekday':
            day_filter = "AND day_type = 'weekday'"
        elif day_type == 'weekend':
            day_filter = "AND day_type = 'weekend'"
        
        # Base query for the route
        base_query = f"""
            FROM fhvhv 
            WHERE PULocationID = {pickup_zone} 
            AND DOLocationID = {dropoff_zone}
            {day_filter}
            AND PULocationID != 265
            AND DOLocationID != 265
            AND price_per_mile > 0 
            AND total_fare_amount > 0
            AND duration_minutes > 0
            AND wait_time_minutes >= 0
        """
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Check if we have data for this route
        count_query = f"SELECT COUNT(*) {base_query}"
        cursor.execute(count_query)
        count_result = cursor.fetchone()
        
        if count_result[0] == 0:
            conn.close()
            return jsonify({
                "error": f"No trips found for route {pickup_zone} -> {dropoff_zone}",
                "total_trips": 0
            }), 404
        
        print(f"Found {count_result[0]:,} trips for this route")
        
        # Get summary statistics INCLUDING total fare amount
        summary_query = f"""
            SELECT 
                COUNT(*) as total_trips,
                AVG(duration_minutes) as avg_duration,
                AVG(price_per_mile) as avg_price_mile,
                AVG(total_fare_amount) as avg_total_fare,
                AVG(wait_time_minutes) as avg_wait_time
            {base_query}
        """
        
        cursor.execute(summary_query)
        summary_result = cursor.fetchone()
        
        # Get hourly data INCLUDING total fare amount
        hourly_query = f"""
            SELECT 
                pickup_hour as hour,
                COUNT(*) as volume,
                AVG(price_per_mile) as price_per_mile,
                AVG(total_fare_amount) as total_fare_amount,
                AVG(duration_minutes) as avg_duration,
                AVG(wait_time_minutes) as avg_wait_time
            {base_query}
            GROUP BY pickup_hour
            ORDER BY pickup_hour
        """
        
        cursor.execute(hourly_query)
        hourly_results = cursor.fetchall()
        
        # Get daily data (day of week) INCLUDING total fare amount
        daily_query = f"""
            SELECT 
                day_of_week,
                CASE day_of_week 
                    WHEN 0 THEN 'Monday'
                    WHEN 1 THEN 'Tuesday'
                    WHEN 2 THEN 'Wednesday'
                    WHEN 3 THEN 'Thursday'
                    WHEN 4 THEN 'Friday'
                    WHEN 5 THEN 'Saturday'
                    WHEN 6 THEN 'Sunday'
                END as day_name,
                COUNT(*) as volume,
                AVG(price_per_mile) as price_per_mile,
                AVG(total_fare_amount) as total_fare_amount,
                AVG(duration_minutes) as avg_duration,
                AVG(wait_time_minutes) as avg_wait_time
            {base_query}  
            GROUP BY day_of_week
            ORDER BY day_of_week
        """
        
        cursor.execute(daily_query)
        daily_results = cursor.fetchall()
        
        # Get monthly data INCLUDING total fare amount
        monthly_query = f"""
            SELECT 
                pickup_month,
                CASE pickup_month
                    WHEN 1 THEN 'Jan 2024'
                    WHEN 2 THEN 'Feb 2024'
                    WHEN 3 THEN 'Mar 2024'
                    WHEN 4 THEN 'Apr 2024'
                    WHEN 5 THEN 'May 2024'
                    WHEN 6 THEN 'Jun 2024'
                    WHEN 7 THEN 'Jul 2024'
                    WHEN 8 THEN 'Aug 2024'
                    WHEN 9 THEN 'Sep 2024'
                    WHEN 10 THEN 'Oct 2024'
                    WHEN 11 THEN 'Nov 2024'
                    WHEN 12 THEN 'Dec 2024'
                END as month_name,
                COUNT(*) as volume,
                AVG(price_per_mile) as price_per_mile,
                AVG(total_fare_amount) as total_fare_amount,
                AVG(duration_minutes) as avg_duration,
                AVG(wait_time_minutes) as avg_wait_time
            {base_query}
            GROUP BY pickup_month
            ORDER BY pickup_month
        """
        
        cursor.execute(monthly_query)
        monthly_results = cursor.fetchall()
        
        conn.close()
        
        # Format the response INCLUDING total fare amount
        response = {
            "summary": {
                "total_trips": int(summary_result[0]),
                "avg_duration": float(summary_result[1]) if summary_result[1] else 0,
                "avg_price_mile": float(summary_result[2]) if summary_result[2] else 0,
                "avg_total_fare": float(summary_result[3]) if summary_result[3] else 0,
                "avg_wait_time": float(summary_result[4]) if summary_result[4] else 0
            },
            "hourly": [
                {
                    "hour": int(row[0]),
                    "volume": int(row[1]),
                    "price_per_mile": float(row[2]) if row[2] else 0,
                    "total_fare_amount": float(row[3]) if row[3] else 0,
                    "avg_duration": float(row[4]) if row[4] else 0,
                    "avg_wait_time": float(row[5]) if row[5] else 0
                }
                for row in hourly_results
            ],
            "daily": [
                {
                    "day_of_week": int(row[0]),
                    "day": row[1],
                    "volume": int(row[2]),
                    "price_per_mile": float(row[3]) if row[3] else 0,
                    "total_fare_amount": float(row[4]) if row[4] else 0,
                    "avg_duration": float(row[5]) if row[5] else 0,
                    "avg_wait_time": float(row[6]) if row[6] else 0
                }
                for row in daily_results
            ],
            "monthly": [
                {
                    "month": int(row[0]),
                    "month_name": row[1],
                    "volume": int(row[2]),
                    "price_per_mile": float(row[3]) if row[3] else 0,
                    "total_fare_amount": float(row[4]) if row[4] else 0,
                    "avg_duration": float(row[5]) if row[5] else 0,
                    "avg_wait_time": float(row[6]) if row[6] else 0
                }
                for row in monthly_results
            ]
        }
        
        return jsonify(response)
        
    except Exception as e:
        print(f"Route analysis error: {str(e)}")
        print(traceback.format_exc())
        return jsonify({"error": str(e)}), 500

# NEW API ENDPOINTS FOR HIGH IMPACT ROUTES
@app.route('/api/high-impact-routes')
def high_impact_routes_combined():
    """
    Return top 10 routes filtered by both day_of_week and pickup_hour.
    Produces two lists: by volume and by avg_total_fare ("price").
    """
    try:
        day = request.args.get('day', type=int)     # 0=Mon ... 6=Sun
        hour = request.args.get('hour', type=int)   # 0..23

        if day is None or day < 0 or day > 6:
            return jsonify({"error": "Invalid day. Must be 0-6 (0=Monday)"}), 400
        if hour is None or hour < 0 or hour > 23:
            return jsonify({"error": "Invalid hour. Must be 0-23"}), 400

        day_names = ['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday']
        day_name = day_names[day]

        conn = get_db_connection()
        cursor = conn.cursor()

        base_sql = """
            FROM fhvhv
            WHERE day_of_week = ?
              AND pickup_hour = ?
              AND PULocationID != 265
              AND DOLocationID != 265
              AND total_fare_amount > 0
              AND duration_minutes > 0
            GROUP BY PULocationID, DOLocationID
            HAVING COUNT(*) >= 5
        """

        # Top 10 by volume (ties broken by total revenue)
        q_volume = f"""
            SELECT 
                PULocationID,
                DOLocationID,
                COUNT(*) as volume,
                AVG(total_fare_amount) as avg_total_fare,
                SUM(total_fare_amount) as total_revenue,
                AVG(duration_minutes) as avg_duration,
                AVG(trip_miles) as avg_distance
            {base_sql}
            ORDER BY volume DESC, total_revenue DESC
            LIMIT 10
        """

        # Top 10 by price (avg_total_fare), ties broken by volume
        q_price = f"""
            SELECT 
                PULocationID,
                DOLocationID,
                COUNT(*) as volume,
                AVG(total_fare_amount) as avg_total_fare,
                SUM(total_fare_amount) as total_revenue,
                AVG(duration_minutes) as avg_duration,
                AVG(trip_miles) as avg_distance
            {base_sql}
            ORDER BY avg_total_fare DESC, volume DESC
            LIMIT 10
        """

        cursor.execute(q_volume, (day, hour))
        volume_rows = cursor.fetchall()

        cursor.execute(q_price, (day, hour))
        price_rows = cursor.fetchall()
        conn.close()

        zone_names = get_zone_names()

        def pack(rows):
            out = []
            for r in rows:
                pu = int(r[0]); do = int(r[1])
                out.append({
                    "pickup_zone": pu,
                    "dropoff_zone": do,
                    "pickup_name": zone_names.get(pu, f"Zone {pu}"),
                    "dropoff_name": zone_names.get(do, f"Zone {do}"),
                    "volume": int(r[2]),
                    "avg_total_fare": float(r[3]) if r[3] else 0.0,
                    "total_revenue": float(r[4]) if r[4] else 0.0,
                    "avg_duration": float(r[5]) if r[5] else 0.0,
                    "avg_distance": float(r[6]) if r[6] else 0.0,
                    "route_name": f"{zone_names.get(pu, f'Zone {pu}')} → {zone_names.get(do, f'Zone {do}')}"
                })
            return out

        return jsonify({
            "day": day,
            "day_name": day_name,
            "hour": hour,
            "time_label": f"{hour}:00",
            "top_by_volume": pack(volume_rows),
            "top_by_price": pack(price_rows)
        })

    except Exception as e:
        print(f"High impact combined error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/high-impact-routes-by-month')
def high_impact_routes_by_month():
    """
    Get top 10 routes for a specific month.
    Returns two lists:
      - top_by_volume: ORDER BY volume DESC, total_revenue DESC
      - top_by_price:  ORDER BY total_revenue DESC, volume DESC
    """
    try:
        month = request.args.get('month', type=int)
        if month is None or month < 1 or month > 12:
            return jsonify({"error": "Invalid month. Must be between 1-12"}), 400

        month_names = [
            'January', 'February', 'March', 'April', 'May', 'June',
            'July', 'August', 'September', 'October', 'November', 'December'
        ]
        month_name = month_names[month - 1]
        print(f"Analyzing high impact routes for month: {month_name}")

        conn = get_db_connection()
        cursor = conn.cursor()

        base_sql = """
            FROM fhvhv
            WHERE pickup_month = ?
              AND PULocationID != 265
              AND DOLocationID != 265
              AND total_fare_amount > 0
              AND duration_minutes > 0
            GROUP BY PULocationID, DOLocationID
            HAVING COUNT(*) >= 20
        """

        # Top 10 by volume (tie-breaker: total_revenue)
        q_volume = f"""
            SELECT 
                PULocationID,
                DOLocationID,
                COUNT(*) as volume,
                AVG(total_fare_amount) as avg_total_fare,
                SUM(total_fare_amount) as total_revenue,
                AVG(duration_minutes) as avg_duration,
                AVG(trip_miles) as avg_distance
            {base_sql}
            ORDER BY volume DESC, total_revenue DESC
            LIMIT 10
        """

        # Top 10 by total price (revenue) (tie-breaker: volume)
        q_price = f"""
            SELECT 
                PULocationID,
                DOLocationID,
                COUNT(*) as volume,
                AVG(total_fare_amount) as avg_total_fare,
                SUM(total_fare_amount) as total_revenue,
                AVG(duration_minutes) as avg_duration,
                AVG(trip_miles) as avg_distance
            {base_sql}
            ORDER BY total_revenue DESC, volume DESC
            LIMIT 10
        """

        cursor.execute(q_volume, (month,))
        volume_rows = cursor.fetchall()

        cursor.execute(q_price, (month,))
        price_rows = cursor.fetchall()
        conn.close()

        zone_names = get_zone_names()

        def pack(rows):
            out = []
            for r in rows:
                pu = int(r[0]); do = int(r[1])
                out.append({
                    "pickup_zone": pu,
                    "dropoff_zone": do,
                    "pickup_name": zone_names.get(pu, f"Zone {pu}"),
                    "dropoff_name": zone_names.get(do, f"Zone {do}"),
                    "volume": int(r[2]),
                    "avg_total_fare": float(r[3]) if r[3] else 0.0,
                    "total_revenue": float(r[4]) if r[4] else 0.0,
                    "avg_duration": float(r[5]) if r[5] else 0.0,
                    "avg_distance": float(r[6]) if r[6] else 0.0,
                    "route_name": f"{zone_names.get(pu, f'Zone {pu}')} → {zone_names.get(do, f'Zone {do}')}"
                })
            return out

        return jsonify({
            "month": month,
            "month_name": f"{month_name} 2024",
            "top_by_volume": pack(volume_rows),
            "top_by_price": pack(price_rows)
        })

    except Exception as e:
        print(f"High impact routes by month error: {str(e)}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/health')
def health_check():
    """Health check endpoint"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM fhvhv")
        count = cursor.fetchone()[0]
        conn.close()
        
        zones_loaded = os.path.exists(zones_geojson_path)
        
        return jsonify({
            "status": "healthy",
            "total_records": count,
            "database": "connected",
            "zones_loaded": zones_loaded,
            "geopandas_available": GEOPANDAS_AVAILABLE,
            "shapefile_upload": "enabled" if GEOPANDAS_AVAILABLE else "disabled"
        })
    except Exception as e:
        return jsonify({
            "status": "error",
            "error": str(e)
        }), 500

# Dashboard HTML Template - Dark Theme WITH TOTAL FARE CHARTS AND NEW HIGH IMPACT ROUTES TABS
DASHBOARD_HTML = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>NYC Taxi Analytics Dashboard</title>
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }

        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
            background-color: #0a0a0a;
            color: #ffffff;
            line-height: 1.6;
            font-weight: 400;
        }

        .container {
            max-width: 1400px;
            margin: 0 auto;
            padding: 32px 24px;
        }

        .header {
            text-align: center;
            margin-bottom: 48px;
        }

        .header h1 {
            font-size: 2.5rem;
            font-weight: 600;
            color: #ffffff;
            margin-bottom: 8px;
            letter-spacing: -0.025em;
        }

        .header .subtitle {
            font-size: 1.125rem;
            color: #9ca3af;
            font-weight: 400;
        }

        .upload-section {
            background: #111111;
            border: 1px solid #1f2937;
            border-radius: 12px;
            padding: 32px;
            margin-bottom: 32px;
            text-align: center;
        }

        .upload-title {
            font-size: 1.25rem;
            font-weight: 500;
            color: #ffffff;
            margin-bottom: 16px;
        }

        .upload-description {
            color: #9ca3af;
            margin-bottom: 24px;
            font-size: 0.875rem;
        }

        .file-upload-container {
            position: relative;
            display: inline-block;
        }

        .file-upload-input {
            position: absolute;
            opacity: 0;
            width: 100%;
            height: 100%;
            cursor: pointer;
        }

        .file-upload-button {
            background: #ffffff;
            color: #000000;
            border: none;
            border-radius: 8px;
            padding: 12px 24px;
            font-size: 0.875rem;
            font-weight: 500;
            cursor: pointer;
            transition: all 0.2s ease;
            display: inline-flex;
            align-items: center;
            gap: 8px;
        }

        .file-upload-button:hover {
            background: #f3f4f6;
            transform: translateY(-1px);
        }

        .controls {
            background: #111111;
            border: 1px solid #1f2937;
            border-radius: 12px;
            padding: 32px;
            margin-bottom: 32px;
        }

        .controls h2 {
            font-size: 1.5rem;
            font-weight: 500;
            color: #ffffff;
            margin-bottom: 24px;
        }

        .map-container {
            background: #111111;
            border: 1px solid #1f2937;
            border-radius: 12px;
            padding: 32px;
            margin-bottom: 32px;
        }

        .map-container h3 {
            font-size: 1.25rem;
            font-weight: 500;
            color: #ffffff;
            margin-bottom: 24px;
        }

        #map {
            height: 500px;
            border-radius: 8px;
            border: 1px solid #1f2937;
        }

        .selection-info {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 16px;
            margin-top: 24px;
        }

        .info-card {
            background: #1f2937;
            border: 1px solid #374151;
            border-radius: 8px;
            padding: 20px;
            text-align: center;
        }

        .info-card h3 {
            font-size: 0.75rem;
            font-weight: 500;
            color: #9ca3af;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            margin-bottom: 8px;
        }

        .info-card .value {
            font-size: 1.5rem;
            font-weight: 600;
            color: #ffffff;
        }

        .filter-controls {
            display: flex;
            gap: 16px;
            align-items: flex-end;
            margin-bottom: 24px;
            flex-wrap: wrap;
        }

        .filter-group {
            display: flex;
            flex-direction: column;
            gap: 8px;
        }

        .filter-group label {
            font-size: 0.875rem;
            font-weight: 500;
            color: #9ca3af;
        }

        select, button {
            padding: 12px 16px;
            border: 1px solid #374151;
            border-radius: 8px;
            font-size: 0.875rem;
            background: #1f2937;
            color: #ffffff;
            transition: all 0.2s ease;
        }

        select:focus {
            outline: none;
            border-color: #6366f1;
            box-shadow: 0 0 0 3px rgba(99, 102, 241, 0.1);
        }

        .analyze-btn {
            background: #ffffff;
            color: #000000;
            border: none;
            padding: 12px 24px;
            font-weight: 500;
            cursor: pointer;
            font-size: 0.875rem;
            transition: all 0.2s ease;
        }

        .analyze-btn:hover {
            background: #f3f4f6;
            transform: translateY(-1px);
        }

        .analyze-btn:disabled {
            background: #374151;
            color: #6b7280;
            cursor: not-allowed;
            transform: none;
        }

        .tabs {
            background: #111111;
            border: 1px solid #1f2937;
            border-radius: 12px;
            margin-bottom: 32px;
            overflow: hidden;
        }

        .tab-headers {
            display: flex;
            border-bottom: 1px solid #1f2937;
            flex-wrap: wrap;
        }

        .tab-header {
            flex: 1;
            padding: 16px 24px;
            text-align: center;
            cursor: pointer;
            background: transparent;
            border: none;
            font-size: 0.875rem;
            font-weight: 500;
            color: #9ca3af;
            transition: all 0.2s ease;
            min-width: 120px;
        }

        .tab-header.active {
            background: #1f2937;
            color: #ffffff;
        }

        .tab-content {
            display: none;
            padding: 32px;
        }

        .tab-content.active {
            display: block;
        }

        .charts-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(400px, 1fr));
            gap: 24px;
        }

        .chart-container {
            background: #1f2937;
            border: 1px solid #374151;
            border-radius: 8px;
            padding: 24px;
            position: relative;
        }

        .chart-title {
            text-align: center;
            font-weight: 500;
            color: #ffffff;
            margin-bottom: 20px;
            font-size: 1rem;
        }

        .chart-canvas {
            width: 100% !important;
            height: 300px !important;
        }

        /* High Impact Routes Styles */
        .high-impact-controls {
            display: flex;
            gap: 16px;
            align-items: center;
            margin-bottom: 24px;
            flex-wrap: wrap;
        }

        .routes-table {
            width: 100%;
            border-collapse: collapse;
            background: #1f2937;
            border-radius: 8px;
            overflow: hidden;
        }

        .routes-table th {
            background: #374151;
            color: #ffffff;
            font-weight: 500;
            padding: 16px 12px;
            text-align: left;
            font-size: 0.875rem;
            border-bottom: 1px solid #4b5563;
        }

        .routes-table td {
            padding: 12px;
            border-bottom: 1px solid #374151;
            color: #9ca3af;
            font-size: 0.875rem;
        }

        .routes-table tr:hover {
            background: #374151;
        }

        .route-rank {
            color: #ffffff;
            font-weight: 600;
            font-size: 1rem;
        }

        .route-name {
            color: #ffffff;
            font-weight: 500;
        }

        .volume-cell {
            color: #34d399;
            font-weight: 600;
        }

        .revenue-cell {
            color: #60a5fa;
            font-weight: 600;
        }

        .loading {
            display: none;
            text-align: center;
            padding: 48px;
            color: #9ca3af;
        }

        .loading.show {
            display: block;
        }

        .spinner {
            border: 3px solid #1f2937;
            border-top: 3px solid #ffffff;
            border-radius: 50%;
            width: 32px;
            height: 32px;
            animation: spin 1s linear infinite;
            margin: 0 auto 16px;
        }

        @keyframes spin {
            0% { transform: rotate(0deg); }
            100% { transform: rotate(360deg); }
        }

        .message {
            padding: 16px;
            border-radius: 8px;
            margin: 16px 0;
            display: none;
            font-size: 0.875rem;
        }

        .error {
            background: #1f1416;
            color: #f87171;
            border: 1px solid #374151;
        }

        .success {
            background: #14221f;
            color: #34d399;
            border: 1px solid #374151;
        }

        .info {
            background: #1e1f36;
            color: #60a5fa;
            border: 1px solid #374151;
        }

        .upload-status {
            margin-top: 16px;
            font-size: 0.875rem;
        }

        .no-data {
            text-align: center;
            padding: 48px;
            color: #6b7280;
        }

        @media (max-width: 768px) {
            .container {
                padding: 16px;
            }
            
            .charts-grid {
                grid-template-columns: 1fr;
            }
            
            .filter-controls {
                flex-direction: column;
                align-items: stretch;
            }
            
            .tab-headers {
                flex-direction: column;
            }
            
            .selection-info {
                grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
            }

            .high-impact-controls {
                flex-direction: column;
                align-items: stretch;
            }

            .routes-table {
                font-size: 0.75rem;
            }

            .routes-table th,
            .routes-table td {
                padding: 8px 6px;
            }
        }

        /* Leaflet Dark Theme */
        .leaflet-container {
            background: #0a0a0a;
        }

        .leaflet-popup-content-wrapper {
            background: #1f2937;
            color: #ffffff;
            border-radius: 8px;
        }

        .leaflet-popup-tip {
            background: #1f2937;
        }

        .leaflet-control-zoom a {
            background-color: #1f2937;
            border-color: #374151;
            color: #ffffff;
        }

        .leaflet-control-zoom a:hover {
            background-color: #374151;
        }

        .leaflet-control-attribution {
            background: rgba(31, 41, 55, 0.8);
            color: #9ca3af;
        }

        .leaflet-control-attribution a {
            color: #60a5fa;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>NYC Taxi Analytics Dashboard</h1>
            <p class="subtitle">Analyze real-time taxi trip data with interactive visualizations</p>
        </div>

        <div class="upload-section">
            <div class="upload-title">Upload Taxi Zones Shapefile</div>
            <div class="upload-description">Upload a ZIP file containing the taxi zones shapefile (.shp, .shx, .dbf, .prj files)</div>
            <div class="file-upload-container">
                <input type="file" id="shapefileInput" class="file-upload-input" accept=".zip" />
                <button class="file-upload-button">
                    <span>Choose File</span>
                </button>
            </div>
            <div class="upload-status" id="uploadStatus"></div>
        </div>

        <div class="controls">
            <h2>Route Analysis</h2>
            <div class="filter-controls">
                <div class="filter-group">
                    <label for="dayType">Day Type</label>
                    <select id="dayType">
                        <option value="all">All Days</option>
                        <option value="weekday">Weekdays</option>
                        <option value="weekend">Weekends</option>
                    </select>
                </div>
                <button class="analyze-btn" id="analyzeBtn" disabled>
                    Analyze Route
                </button>
            </div>
            
            <div class="message error" id="errorMsg"></div>
            <div class="message success" id="successMsg"></div>
            <div class="message info" id="infoMsg"></div>
        </div>

        <div class="map-container">
            <h3>Select Pickup and Dropoff Zones</h3>
            <div id="map"></div>
            <div class="selection-info">
                <div class="info-card">
                    <h3>Pickup Zone</h3>
                    <div class="value" id="pickupZone">Select on map</div>
                </div>
                <div class="info-card">
                    <h3>Dropoff Zone</h3>
                    <div class="value" id="dropoffZone">Select on map</div>
                </div>
                <div class="info-card">
                    <h3>Total Trips</h3>
                    <div class="value" id="totalTrips">—</div>
                </div>
                <div class="info-card">
                    <h3>Avg Duration</h3>
                    <div class="value" id="avgDuration">—</div>
                </div>
                <div class="info-card">
                    <h3>Avg Price/Mile</h3>
                    <div class="value" id="avgPriceMile">—</div>
                </div>
                <div class="info-card">
                    <h3>Avg Total Fare</h3>
                    <div class="value" id="avgTotalFare">—</div>
                </div>
                <div class="info-card">
                    <h3>Avg Wait Time</h3>
                    <div class="value" id="avgWaitTime">—</div>
                </div>
            </div>
        </div>

        <div class="loading" id="loadingIndicator">
            <div class="spinner"></div>
            <h3>Processing data...</h3>
            <p>Analyzing trip patterns between selected zones</p>
        </div>

        <div class="tabs" id="chartsTabs" style="display: none;">
            <div class="tab-headers">
                <button class="tab-header active" data-tab="hourly">Hourly Analysis</button>
                <button class="tab-header" data-tab="daily">Daily Analysis</button>
                <button class="tab-header" data-tab="monthly">Monthly Analysis</button>
               <button class="tab-header" data-tab="high-impact-combined">High Impact by Day & Hour</button>
                <button class="tab-header" data-tab="high-impact-month">High Impact Routes by Month</button>
            </div>

            <div class="tab-content active" id="hourly">
                <div class="charts-grid">
                    <div class="chart-container">
                        <div class="chart-title">Trip Volume by Hour</div>
                        <canvas class="chart-canvas" id="hourlyVolumeChart"></canvas>
                    </div>
                    <div class="chart-container">
                        <div class="chart-title">Price per Mile by Hour</div>
                        <canvas class="chart-canvas" id="hourlyPriceChart"></canvas>
                    </div>
                    <div class="chart-container">
                        <div class="chart-title">Total Price by Hour (2024)</div>
                        <canvas class="chart-canvas" id="hourlyTotalFareChart"></canvas>
                    </div>
                    <div class="chart-container">
                        <div class="chart-title">Average Duration by Hour</div>
                        <canvas class="chart-canvas" id="hourlyDurationChart"></canvas>
                    </div>
                    <div class="chart-container">
                        <div class="chart-title">Average Wait Time by Hour</div>
                        <canvas class="chart-canvas" id="hourlyWaitChart"></canvas>
                    </div>
                </div>
            </div>

            <div class="tab-content" id="daily">
                <div class="charts-grid">
                    <div class="chart-container">
                        <div class="chart-title">Trip Volume by Day of Week</div>
                        <canvas class="chart-canvas" id="dailyVolumeChart"></canvas>
                    </div>
                    <div class="chart-container">
                        <div class="chart-title">Price per Mile by Day of Week</div>
                        <canvas class="chart-canvas" id="dailyPriceChart"></canvas>
                    </div>
                    <div class="chart-container">
                        <div class="chart-title">Total Price by Day (2024)</div>
                        <canvas class="chart-canvas" id="dailyTotalFareChart"></canvas>
                    </div>
                    <div class="chart-container">
                        <div class="chart-title">Average Duration by Day of Week</div>
                        <canvas class="chart-canvas" id="dailyDurationChart"></canvas>
                    </div>
                    <div class="chart-container">
                        <div class="chart-title">Average Wait Time by Day of Week</div>
                        <canvas class="chart-canvas" id="dailyWaitChart"></canvas>
                    </div>
                </div>
            </div>

            <div class="tab-content" id="monthly">
                <div class="charts-grid">
                    <div class="chart-container">
                        <div class="chart-title">Trip Volume by Month (2024)</div>
                        <canvas class="chart-canvas" id="monthlyVolumeChart"></canvas>
                    </div>
                    <div class="chart-container">
                        <div class="chart-title">Price per Mile by Month (2024)</div>
                        <canvas class="chart-canvas" id="monthlyPriceChart"></canvas>
                    </div>
                    <div class="chart-container">
                        <div class="chart-title">Total Price by Month (2024)</div>
                        <canvas class="chart-canvas" id="monthlyTotalFareChart"></canvas>
                    </div>
                    <div class="chart-container">
                        <div class="chart-title">Average Duration by Month (2024)</div>
                        <canvas class="chart-canvas" id="monthlyDurationChart"></canvas>
                    </div>
                    <div class="chart-container">
                        <div class="chart-title">Average Wait Time by Month (2024)</div>
                        <canvas class="chart-canvas" id="monthlyWaitChart"></canvas>
                    </div>
                </div>
            </div>

            <!-- NEW HIGH IMPACT ROUTES TABS -->
            <div class="tab-content" id="high-impact-combined">
                <div class="high-impact-controls">
                    <div class="filter-group">
                        <label for="dayHourDaySelect">Select Day</label>
                        <select id="dayHourDaySelect">
                            <option value="">Choose a day...</option>
                            <option value="0">Monday</option>
                            <option value="1">Tuesday</option>
                            <option value="2">Wednesday</option>
                            <option value="3">Thursday</option>
                            <option value="4">Friday</option>
                            <option value="5">Saturday</option>
                            <option value="6">Sunday</option>
                        </select>
                    </div>
                    <div class="filter-group">
                        <label for="dayHourHourSelect">Select Hour</label>
                        <select id="dayHourHourSelect">
                            <option value="">Choose an hour...</option>
                            <option value="0">12:00 AM</option>
                            <option value="1">1:00 AM</option>
                            <option value="2">2:00 AM</option>
                            <option value="3">3:00 AM</option>
                            <option value="4">4:00 AM</option>
                            <option value="5">5:00 AM</option>
                            <option value="6">6:00 AM</option>
                            <option value="7">7:00 AM</option>
                            <option value="8">8:00 AM</option>
                            <option value="9">9:00 AM</option>
                            <option value="10">10:00 AM</option>
                            <option value="11">11:00 AM</option>
                            <option value="12">12:00 PM</option>
                            <option value="13">1:00 PM</option>
                            <option value="14">2:00 PM</option>
                            <option value="15">3:00 PM</option>
                            <option value="16">4:00 PM</option>
                            <option value="17">5:00 PM</option>
                            <option value="18">6:00 PM</option>
                            <option value="19">7:00 PM</option>
                            <option value="20">8:00 PM</option>
                            <option value="21">9:00 PM</option>
                            <option value="22">10:00 PM</option>
                            <option value="23">11:00 PM</option>
                        </select>
                    </div>
                    <button class="analyze-btn" onclick="analyzeHighImpactByDayHour()">Get Top Routes</button>
                </div>

                <div id="highImpactCombinedHeader" style="margin-bottom: 16px; color:#9ca3af;"></div>

                <div id="highImpactCombinedVolume"></div>
                <div style="height:16px;"></div>
                <div id="highImpactCombinedPrice"></div>
            </div>

            <div class="tab-content" id="high-impact-month">
            <div class="high-impact-controls">
                <div class="filter-group">
                <label for="monthSelect">Select Month</label>
                <select id="monthSelect">
                    <option value="">Choose a month...</option>
                    <option value="1">January 2024</option>
                    <option value="2">February 2024</option>
                    <option value="3">March 2024</option>
                    <option value="4">April 2024</option>
                    <option value="5">May 2024</option>
                    <option value="6">June 2024</option>
                    <option value="7">July 2024</option>
                    <option value="8">August 2024</option>
                    <option value="9">September 2024</option>
                    <option value="10">October 2024</option>
                    <option value="11">November 2024</option>
                    <option value="12">December 2024</option>
                </select>
                </div>
                <button class="analyze-btn" onclick="analyzeHighImpactByMonth()">Get Top Routes</button>
            </div>

            <div id="highImpactMonthHeader" style="margin-bottom: 16px; color:#9ca3af;"></div>
            <div id="highImpactMonthVolume"></div>
            <div style="height:16px;"></div>
            <div id="highImpactMonthPrice"></div>
            </div>

    <script>
        // Global variables
        let map;
        let zonesLayer;
        let pickupZoneId = null;
        let dropoffZoneId = null;
        let charts = {};

        // Initialize the application
        async function initApp() {
            try {
                showMessage('Initializing dashboard...', 'info');
                initMap();
                setupEventListeners();
                await checkHealth();
                await loadTaxiZones();
                showMessage('Dashboard ready! Upload a shapefile or select zones on the map.', 'success');
            } catch (error) {
                console.error('Initialization error:', error);
                showMessage('Error initializing dashboard: ' + error.message, 'error');
            }
        }

        // Initialize map
        function initMap() {
            map = L.map('map').setView([40.7589, -73.9851], 11);
            
            // Dark tile layer
            L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
                attribution: '© OpenStreetMap contributors, © CARTO'
            }).addTo(map);
        }

        // Load taxi zones from API
        async function loadTaxiZones() {
            try {
                const response = await fetch('/api/taxi-zones');
                const zonesData = await response.json();
                
                if (zonesData.features && zonesData.features.length > 0) {
                    displayZones(zonesData);
                    showMessage(`Loaded ${zonesData.features.length} taxi zones`, 'success');
                } else {
                    showMessage('No zones loaded. Please upload a shapefile.', 'info');
                }
            } catch (error) {
                console.error('Error loading taxi zones:', error);
                showMessage('Error loading zones: ' + error.message, 'error');
            }
        }

        // Display zones on map
        function displayZones(geojsonData) {
            if (zonesLayer) {
                map.removeLayer(zonesLayer);
            }

            zonesLayer = L.geoJSON(geojsonData, {
                style: {
                    fillColor: '#ffffff',
                    weight: 1,
                    opacity: 0.8,
                    color: '#ffffff',
                    fillOpacity: 0.1
                },
                pointToLayer: function(feature, latlng) {
                    return L.circleMarker(latlng, {
                        radius: 8,
                        fillColor: '#ffffff',
                        color: '#ffffff',
                        weight: 2,
                        opacity: 0.8,
                        fillOpacity: 0.3
                    });
                },
                onEachFeature: function(feature, layer) {
                    // Get zone ID from properties (try common field names)
                    const zoneId = feature.properties.LocationID || 
                                  feature.properties.OBJECTID || 
                                  feature.properties.zone_id || 
                                  feature.properties.id ||
                                  feature.properties.Zone;
                    
                    const zoneName = feature.properties.zone || 
                                    feature.properties.Zone || 
                                    feature.properties.borough || 
                                    feature.properties.name ||
                                    `Zone ${zoneId}`;

                    // Bind popup
                    layer.bindPopup(`<b>Zone ${zoneId}</b><br>${zoneName}`);
                    
                    // Hover effects
                    layer.on('mouseover', function(e) {
                        if (layer.setStyle) {
                            layer.setStyle({
                                weight: 2,
                                fillOpacity: 0.3
                            });
                        } else {
                            // Handle circle markers
                            layer.setStyle({
                                radius: 10,
                                fillOpacity: 0.6
                            });
                        }
                        
                        // Show tooltip
                        layer.openPopup();
                    });
                    
                    layer.on('mouseout', function(e) {
                        if (layer.setStyle) {
                            layer.setStyle({
                                weight: 1,
                                fillOpacity: 0.1
                            });
                        } else {
                            // Handle circle markers
                            layer.setStyle({
                                radius: 8,
                                fillOpacity: 0.3
                            });
                        }
                        
                        layer.closePopup();
                    });
                    
                    // Click handler
                    layer.on('click', function(e) {
                        if (zoneId) {
                            selectZone(parseInt(zoneId), zoneName);
                        }
                    });
                }
            }).addTo(map);

            // Fit map to zones
            map.fitBounds(zonesLayer.getBounds());
        }

        // Upload shapefile
        async function uploadShapefile(file) {
            const formData = new FormData();
            formData.append('shapefile', file);

            try {
                showMessage('Uploading and processing shapefile...', 'info');
                
                const response = await fetch('/upload-zones', {
                    method: 'POST',
                    body: formData
                });

                const result = await response.json();
                
                if (response.ok) {
                    showMessage(`Shapefile processed successfully! Loaded ${result.zones_count} zones.`, 'success');
                    await loadTaxiZones();
                } else {
                    throw new Error(result.error);
                }
            } catch (error) {
                showMessage('Upload failed: ' + error.message, 'error');
            }
        }

        // Setup event listeners
        function setupEventListeners() {
            document.getElementById('analyzeBtn').addEventListener('click', analyzeRoute);
            
            // File upload
            document.getElementById('shapefileInput').addEventListener('change', function(e) {
                const file = e.target.files[0];
                if (file) {
                    document.getElementById('uploadStatus').textContent = `Selected: ${file.name}`;
                    uploadShapefile(file);
                }
            });
            
            // Tab switching
            document.querySelectorAll('.tab-header').forEach(tab => {
                tab.addEventListener('click', function() {
                    switchTab(this.dataset.tab);
                });
            });
        }

        // Select zone function
        function selectZone(zoneId, zoneName) {
            if (!pickupZoneId) {
                pickupZoneId = zoneId;
                document.getElementById('pickupZone').textContent = `${zoneId}: ${zoneName}`;
                showMessage(`Pickup zone selected: ${zoneName}`, 'success');
            } else if (!dropoffZoneId) {
                // allow same zone for dropoff
                dropoffZoneId = zoneId;
                document.getElementById('dropoffZone').textContent = `${zoneId}: ${zoneName}`;
                document.getElementById('analyzeBtn').disabled = false;
                const same = (pickupZoneId === dropoffZoneId) ? ' (same as pickup)' : '';
                showMessage(`Dropoff zone selected: ${zoneName}${same}. Click "Analyze Route" to process data.`, 'success');
            } else if (zoneId === pickupZoneId && zoneId === dropoffZoneId) {
            // both already same—nudge user to analyze or reselect
            showMessage('Pickup and dropoff are the same. Click Analyze Route or pick new zones.', 'info');
            } else {
                // Reset and start over
                pickupZoneId = zoneId;
                dropoffZoneId = null;
                document.getElementById('pickupZone').textContent = `${zoneId}: ${zoneName}`;
                document.getElementById('dropoffZone').textContent = 'Select on map';
                document.getElementById('analyzeBtn').disabled = true;
                clearSummaryStats();
                showMessage(`New pickup zone selected: ${zoneName}. Select a dropoff zone.`, 'success');
            }
        }

        // Switch tabs
        function switchTab(tabName) {
            document.querySelectorAll('.tab-header').forEach(t => t.classList.remove('active'));
            document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
            
            document.querySelector(`[data-tab="${tabName}"]`).classList.add('active');
            document.getElementById(tabName).classList.add('active');
        }

        // Check backend health and update UI accordingly
        async function checkHealth() {
            try {
                const response = await fetch('/api/health');
                const health = await response.json();
                if (health.status === 'healthy') {
                    showMessage(`Connected to database with ${health.total_records.toLocaleString()} records`, 'success');
                    
                    // Hide upload section if geopandas is not available
                    if (!health.geopandas_available) {
                        document.querySelector('.upload-section').style.display = 'none';
                        showMessage('Shapefile upload disabled - using default zones. To enable upload, install: pip install geopandas', 'info');
                        // Load some default zones for demo
                        loadDefaultZones();
                    }
                } else {
                    throw new Error('Backend not ready');
                }
            } catch (error) {
                showMessage('Backend connection failed: ' + error.message, 'error');
                throw error;
            }
        }

        // Load default zones if geopandas is not available
        function loadDefaultZones() {
            const defaultZones = {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {
                            "LocationID": 1,
                            "zone": "Newark Airport"
                        },
                        "geometry": {
                            "type": "Point",
                            "coordinates": [-74.1745, 40.6895]
                        }
                    },
                    {
                        "type": "Feature", 
                        "properties": {
                            "LocationID": 48,
                            "zone": "Clinton East"
                        },
                        "geometry": {
                            "type": "Point",
                            "coordinates": [-73.9924, 40.7614]
                        }
                    },
                    {
                        "type": "Feature",
                        "properties": {
                            "LocationID": 127,
                            "zone": "JFK Airport"
                        },
                        "geometry": {
                            "type": "Point",
                            "coordinates": [-73.7781, 40.6413]
                        }
                    },
                    {
                        "type": "Feature",
                        "properties": {
                            "LocationID": 133,
                            "zone": "LaGuardia Airport"
                        },
                        "geometry": {
                            "type": "Point",
                            "coordinates": [-73.8740, 40.7769]
                        }
                    },
                    {
                        "type": "Feature",
                        "properties": {
                            "LocationID": 230,
                            "zone": "Upper East Side South"
                        },
                        "geometry": {
                            "type": "Point",
                            "coordinates": [-73.9565, 40.7690]
                        }
                    },
                    {
                        "type": "Feature",
                        "properties": {
                            "LocationID": 261,
                            "zone": "World Trade Center"
                        },
                        "geometry": {
                            "type": "Point",
                            "coordinates": [-74.0125, 40.7116]
                        }
                    }
                ]
            };
            
            displayZones(defaultZones);
            showMessage('Loaded default zones for demo. Upload shapefile for full zone coverage.', 'info');
        }

        // Analyze route with REAL data
        async function analyzeRoute() {
            if (!pickupZoneId || !dropoffZoneId) {
                showMessage('Please select both pickup and dropoff zones', 'error');
                return;
            }

            document.getElementById('loadingIndicator').classList.add('show');
            document.getElementById('chartsTabs').style.display = 'none';

            try {
                const dayType = document.getElementById('dayType').value;
                
                showMessage('Querying database...', 'info');
                
                const response = await fetch(`/api/route-analysis?pickup=${pickupZoneId}&dropoff=${dropoffZoneId}&day_type=${dayType}`);
                
                if (!response.ok) {
                    const errorData = await response.json();
                    throw new Error(errorData.error || 'Failed to analyze route');
                }
                
                const routeData = await response.json();
                
                // Update summary statistics
                updateSummaryStats(routeData.summary);
                
                // Create charts with REAL data
                createHourlyCharts(routeData.hourly);
                createDailyCharts(routeData.daily);
                createMonthlyCharts(routeData.monthly);
                
                document.getElementById('loadingIndicator').classList.remove('show');
                document.getElementById('chartsTabs').style.display = 'block';
                
                showMessage(`Analysis complete! Processed ${routeData.summary.total_trips.toLocaleString()} trips.`, 'success');
                
            } catch (error) {
                console.error('Analysis error:', error);
                document.getElementById('loadingIndicator').classList.remove('show');
                
                if (error.message.includes('No trips found')) {
                    showMessage(`${error.message}. Try selecting different zones or changing the day type filter.`, 'error');
                } else {
                    showMessage('Error processing data: ' + error.message, 'error');
                }
            }
        }

        // NEW HIGH IMPACT ROUTES FUNCTIONS
        async function analyzeHighImpactByDayHour() {
            const day = document.getElementById('dayHourDaySelect').value;
            const hour = document.getElementById('dayHourHourSelect').value;

            if ((day === '' || day === null) || (hour === '' || hour === null)) {
                showMessage('Please select both a day and an hour', 'error');
                return;
            }

            const header = document.getElementById('highImpactCombinedHeader');
            const volDiv = document.getElementById('highImpactCombinedVolume');
            const priceDiv = document.getElementById('highImpactCombinedPrice');

            const loadingHTML = '<div class="loading show"><div class="spinner"></div><p>Loading top routes...</p></div>';
            volDiv.innerHTML = loadingHTML;
            priceDiv.innerHTML = loadingHTML;
            header.textContent = '';

            try {
                const resp = await fetch(`/api/high-impact-routes?day=${day}&hour=${hour}`);
                const data = await resp.json();
                if (!resp.ok) throw new Error(data.error || 'Failed to load routes');

                header.textContent = `Top routes for ${data.day_name} at ${String(data.hour).padStart(2,'0')}:00`;

                // Table 1: Top by Volume
                volDiv.innerHTML = renderHighImpactTable(
                    data.top_by_volume,
                    'Top 10 Routes by Volume',
                    true // show volume emphasis
                );

                // Table 2: Top by Price (Avg Fare)
                priceDiv.innerHTML = renderHighImpactTable(
                    data.top_by_price,
                    'Top 10 Routes by Price (Avg Fare)',
                    false // show price emphasis
                );

                showMessage(`Loaded ${data.top_by_volume.length} by volume and ${data.top_by_price.length} by price.`, 'success');
            } catch (e) {
                volDiv.innerHTML = `<div class="no-data">${e.message}</div>`;
                priceDiv.innerHTML = `<div class="no-data">${e.message}</div>`;
                showMessage('Error loading high impact routes: ' + e.message, 'error');
            }
        }

        // Reuse your existing table style; render two different emphases.
        function renderHighImpactTable(routes, title, emphasizeVolume) {
            if (!routes || routes.length === 0) {
                return '<div class="no-data">No routes found for the selected day & hour.</div>';
            }

            let html = `
                <div style="margin-bottom: 12px;">
                    <h3 style="color:#ffffff; margin-bottom:6px;">${title}</h3>
                    <p style="color:#9ca3af; font-size:0.875rem;">Ranked ${emphasizeVolume ? 'by trip volume' : 'by average fare'}</p>
                </div>
                <table class="routes-table">
                    <thead>
                        <tr>
                            <th>Rank</th>
                            <th style="min-width:250px;">Route</th>
                            <th>Volume</th>
                            <th>Total Revenue</th>
                            <th>Avg Fare</th>
                            <th>Avg Duration</th>
                            <th>Avg Distance</th>
                        </tr>
                    </thead>
                    <tbody>
            `;

            routes.forEach((r, i) => {
                html += `
                    <tr>
                        <td class="route-rank">#${i + 1}</td>
                        <td class="route-name" style="max-width:300px; word-wrap:break-word;">${r.route_name || (r.pickup_name + ' → ' + r.dropoff_name)}</td>
                        <td class="volume-cell">${(r.volume || 0).toLocaleString()}</td>
                        <td class="revenue-cell">${(r.total_revenue || 0).toLocaleString(undefined, {minimumFractionDigits:0, maximumFractionDigits:0})}</td>
                        <td>${(r.avg_total_fare || 0).toFixed(2)}</td>
                        <td>${(r.avg_duration || 0).toFixed(1)} min</td>
                        <td>${(r.avg_distance || 0).toFixed(1)} mi</td>
                    </tr>
                `;
            });

            html += `</tbody></table>`;
            return html;
        }

        async function analyzeHighImpactByMonth() {
            const month = document.getElementById('monthSelect').value;
            if (!month) {
                showMessage('Please select a month', 'error');
                return;
            }

            const header = document.getElementById('highImpactMonthHeader');
            const volDiv = document.getElementById('highImpactMonthVolume');
            const priceDiv = document.getElementById('highImpactMonthPrice');

            const loadingHTML = '<div class="loading show"><div class="spinner"></div><p>Loading top routes...</p></div>';
            header.textContent = '';
            volDiv.innerHTML = loadingHTML;
            priceDiv.innerHTML = loadingHTML;

            try {
                const resp = await fetch(`/api/high-impact-routes-by-month?month=${month}`);
                const data = await resp.json();
                if (!resp.ok) throw new Error(data.error || 'Failed to load routes');

                header.textContent = `Top routes for ${data.month_name}`;

                // Table 1: Top by Volume
                volDiv.innerHTML = renderHighImpactTable(
                data.top_by_volume || [],
                'Top 10 Routes by Volume (Month)',
                true // emphasize volume
                );

                // Table 2: Top by Total Price (Revenue)
                priceDiv.innerHTML = renderHighImpactTable(
                data.top_by_price || [],
                'Top 10 Routes by Total Price (Revenue) (Month)',
                false // emphasize price
                );

                showMessage(`Loaded ${ (data.top_by_volume||[]).length } by volume and ${ (data.top_by_price||[]).length } by price.`, 'success');
            } catch (error) {
                volDiv.innerHTML = `<div class="no-data">${error.message}</div>`;
                priceDiv.innerHTML = `<div class="no-data">${error.message}</div>`;
                showMessage('Error loading high impact routes: ' + error.message, 'error');
            }
        }


        function displayHighImpactResults(container, data, type) {
            if (!data.routes || data.routes.length === 0) {
                container.innerHTML = '<div class="no-data">No routes found for the selected period.</div>';
                return;
            }

            const timeLabel = data.time_label || data.day_name || data.month_name;
            
            let html = `
                <div style="margin-bottom: 24px;">
                    <h3 style="color: #ffffff; margin-bottom: 8px;">Top 10 Routes for ${timeLabel}</h3>
                    <p style="color: #9ca3af; font-size: 0.875rem;">Ranked by trip volume and total revenue</p>
                </div>
                <table class="routes-table">
                    <thead>
                        <tr>
                            <th>Rank</th>
                            <th style="min-width: 250px;">Route</th>
                            <th>Volume</th>
                            <th>Total Revenue</th>
                            <th>Avg Fare</th>
                            <th>Avg Duration</th>
                            <th>Avg Distance</th>
                        </tr>
                    </thead>
                    <tbody>
            `;

            data.routes.forEach((route, index) => {
                // Use the actual zone names from the API response
                const routeName = route.route_name || `${route.pickup_name} → ${route.dropoff_name}`;
                
                html += `
                    <tr>
                        <td class="route-rank">#${index + 1}</td>
                        <td class="route-name" style="max-width: 300px; word-wrap: break-word;">${routeName}</td>
                        <td class="volume-cell">${route.volume.toLocaleString()}</td>
                        <td class="revenue-cell">${route.total_revenue.toLocaleString(undefined, {minimumFractionDigits: 0, maximumFractionDigits: 0})}</td>
                        <td>${route.avg_total_fare.toFixed(2)}</td>
                        <td>${route.avg_duration.toFixed(1)} min</td>
                        <td>${route.avg_distance.toFixed(1)} mi</td>
                    </tr>
                `;
            });

            html += `
                    </tbody>
                </table>
                <div style="margin-top: 16px; font-size: 0.75rem; color: #6b7280;">
                    * Routes are ranked by trip volume, then by total revenue. Zone names are loaded from uploaded shapefile or default NYC zones.
                </div>
            `;

            container.innerHTML = html;
        }

        // Update summary statistics INCLUDING total fare
        function updateSummaryStats(summary) {
            document.getElementById('totalTrips').textContent = summary.total_trips.toLocaleString();
            document.getElementById('avgDuration').textContent = summary.avg_duration.toFixed(1) + ' min';
            document.getElementById('avgPriceMile').textContent = '$' + summary.avg_price_mile.toFixed(2);
            document.getElementById('avgTotalFare').textContent = '$' + summary.avg_total_fare.toFixed(2);
            document.getElementById('avgWaitTime').textContent = summary.avg_wait_time.toFixed(1) + ' min';
        }

        // Clear summary statistics
        function clearSummaryStats() {
            document.getElementById('totalTrips').textContent = '—';
            document.getElementById('avgDuration').textContent = '—';
            document.getElementById('avgPriceMile').textContent = '—';
            document.getElementById('avgTotalFare').textContent = '—';
            document.getElementById('avgWaitTime').textContent = '—';
        }

        // Create hourly charts INCLUDING total fare chart
        function createHourlyCharts(data) {
            const hourlyData = [];
            for (let hour = 0; hour < 24; hour++) {
                const hourData = data.find(d => d.hour === hour);
                if (hourData) {
                    hourlyData.push(hourData);
                } else {
                    hourlyData.push({
                        hour: hour,
                        volume: 0,
                        price_per_mile: 0,
                        total_fare_amount: 0,
                        avg_duration: 0,
                        avg_wait_time: 0
                    });
                }
            }

            const dataWithValues = data.filter(d => d.volume > 0);
            const hourlyAvgs = {
                volume: dataWithValues.length > 0 ? dataWithValues.reduce((sum, d) => sum + d.volume, 0) / dataWithValues.length : 0,
                price_per_mile: dataWithValues.length > 0 ? dataWithValues.reduce((sum, d) => sum + d.price_per_mile, 0) / dataWithValues.length : 0,
                total_fare_amount: dataWithValues.length > 0 ? dataWithValues.reduce((sum, d) => sum + d.total_fare_amount, 0) / dataWithValues.length : 0,
                avg_duration: dataWithValues.length > 0 ? dataWithValues.reduce((sum, d) => sum + d.avg_duration, 0) / dataWithValues.length : 0,
                avg_wait_time: dataWithValues.length > 0 ? dataWithValues.reduce((sum, d) => sum + d.avg_wait_time, 0) / dataWithValues.length : 0
            };

            createLineChart('hourlyVolumeChart', {
                labels: hourlyData.map(d => d.hour + ':00'),
                datasets: [
                    {
                        label: 'Trip Volume',
                        data: hourlyData.map(d => d.volume),
                        borderColor: '#ffffff',
                        backgroundColor: 'rgba(255, 255, 255, 0.1)',
                        fill: true,
                        tension: 0.4
                    },
                    {
                        label: 'Daily Average',
                        data: new Array(24).fill(hourlyAvgs.volume),
                        borderColor: '#60a5fa',
                        borderDash: [5, 5],
                        fill: false
                    }
                ]
            }, 'Trip Count');

            createLineChart('hourlyPriceChart', {
                labels: hourlyData.map(d => d.hour + ':00'),
                datasets: [
                    {
                        label: 'Price per Mile',
                        data: hourlyData.map(d => d.price_per_mile),
                        borderColor: '#34d399',
                        backgroundColor: 'rgba(52, 211, 153, 0.1)',
                        fill: true,
                        tension: 0.4
                    },
                    {
                        label: 'Daily Average',
                        data: new Array(24).fill(hourlyAvgs.price_per_mile),
                        borderColor: '#60a5fa',
                        borderDash: [5, 5],
                        fill: false
                    }
                ]
            }, 'Price ($)');

            // NEW: Total Fare Amount Chart for Hourly
            createLineChart('hourlyTotalFareChart', {
                labels: hourlyData.map(d => d.hour + ':00'),
                datasets: [
                    {
                        label: 'Actual Total Price',
                        data: hourlyData.map(d => d.total_fare_amount),
                        borderColor: '#06b6d4',
                        backgroundColor: 'rgba(6, 182, 212, 0.1)',
                        fill: true,
                        tension: 0.4
                    },
                    {
                        label: 'Yearly Average',
                        data: new Array(24).fill(hourlyAvgs.total_fare_amount),
                        borderColor: '#60a5fa',
                        borderDash: [5, 5],
                        fill: false
                    }
                ]
            }, 'Price ($)', {
                tooltipCallback: function(context) {
                    const hour = context.label;
                    const price = context.parsed.y.toFixed(2);
                    const yearlyAvg = hourlyAvgs.total_fare_amount.toFixed(2);
                    return [
                        `Time: ${hour}`,
                        `Total Price: $${price}`,
                        `Yearly Average: $${yearlyAvg}`
                    ];
                }
            });

            createLineChart('hourlyDurationChart', {
                labels: hourlyData.map(d => d.hour + ':00'),
                datasets: [
                    {
                        label: 'Duration',
                        data: hourlyData.map(d => d.avg_duration),
                        borderColor: '#f59e0b',
                        backgroundColor: 'rgba(245, 158, 11, 0.1)',
                        fill: true,
                        tension: 0.4
                    },
                    {
                        label: 'Daily Average',
                        data: new Array(24).fill(hourlyAvgs.avg_duration),
                        borderColor: '#60a5fa',
                        borderDash: [5, 5],
                        fill: false
                    }
                ]
            }, 'Duration (minutes)');

            createLineChart('hourlyWaitChart', {
                labels: hourlyData.map(d => d.hour + ':00'),
                datasets: [
                    {
                        label: 'Wait Time',
                        data: hourlyData.map(d => d.avg_wait_time),
                        borderColor: '#ec4899',
                        backgroundColor: 'rgba(236, 72, 153, 0.1)',
                        fill: true,
                        tension: 0.4
                    },
                    {
                        label: 'Daily Average',
                        data: new Array(24).fill(hourlyAvgs.avg_wait_time),
                        borderColor: '#60a5fa',
                        borderDash: [5, 5],
                        fill: false
                    }
                ]
            }, 'Wait Time (minutes)');
        }

        // Create daily charts INCLUDING total fare chart
        function createDailyCharts(data) {
            const dailyAvgs = {
                volume: data.length > 0 ? data.reduce((sum, d) => sum + d.volume, 0) / data.length : 0,
                price_per_mile: data.length > 0 ? data.reduce((sum, d) => sum + d.price_per_mile, 0) / data.length : 0,
                total_fare_amount: data.length > 0 ? data.reduce((sum, d) => sum + d.total_fare_amount, 0) / data.length : 0,
                avg_duration: data.length > 0 ? data.reduce((sum, d) => sum + d.avg_duration, 0) / data.length : 0,
                avg_wait_time: data.length > 0 ? data.reduce((sum, d) => sum + d.avg_wait_time, 0) / data.length : 0
            };

            createLineChart('dailyVolumeChart', {
                labels: data.map(d => d.day),
                datasets: [
                    {
                        label: 'Trip Volume',
                        data: data.map(d => d.volume),
                        borderColor: '#ffffff',
                        backgroundColor: 'rgba(255, 255, 255, 0.1)',
                        fill: true,
                        tension: 0.4
                    },
                    {
                        label: 'Weekly Average',
                        data: new Array(data.length).fill(dailyAvgs.volume),
                        borderColor: '#60a5fa',
                        borderDash: [5, 5],
                        fill: false
                    }
                ]
            }, 'Trip Count');

            createLineChart('dailyPriceChart', {
                labels: data.map(d => d.day),
                datasets: [
                    {
                        label: 'Price per Mile',
                        data: data.map(d => d.price_per_mile),
                        borderColor: '#34d399',
                        backgroundColor: 'rgba(52, 211, 153, 0.1)',
                        fill: true,
                        tension: 0.4
                    },
                    {
                        label: 'Weekly Average',
                        data: new Array(data.length).fill(dailyAvgs.price_per_mile),
                        borderColor: '#60a5fa',
                        borderDash: [5, 5],
                        fill: false
                    }
                ]
            }, 'Price ($)');

            // NEW: Total Fare Amount Chart for Daily
            createLineChart('dailyTotalFareChart', {
                labels: data.map(d => d.day),
                datasets: [
                    {
                        label: 'Actual Total Price',
                        data: data.map(d => d.total_fare_amount),
                        borderColor: '#06b6d4',
                        backgroundColor: 'rgba(6, 182, 212, 0.1)',
                        fill: true,
                        tension: 0.4
                    },
                    {
                        label: 'Yearly Average',
                        data: new Array(data.length).fill(dailyAvgs.total_fare_amount),
                        borderColor: '#60a5fa',
                        borderDash: [5, 5],
                        fill: false
                    }
                ]
            }, 'Price ($)', {
                tooltipCallback: function(context) {
                    const day = context.label;
                    const price = context.parsed.y.toFixed(2);
                    const yearlyAvg = dailyAvgs.total_fare_amount.toFixed(2);
                    return [
                        `Day: ${day}`,
                        `Total Price: $${price}`,
                        `Yearly Average: $${yearlyAvg}`
                    ];
                }
            });

            createLineChart('dailyDurationChart', {
                labels: data.map(d => d.day),
                datasets: [
                    {
                        label: 'Duration',
                        data: data.map(d => d.avg_duration),
                        borderColor: '#f59e0b',
                        backgroundColor: 'rgba(245, 158, 11, 0.1)',
                        fill: true,
                        tension: 0.4
                    },
                    {
                        label: 'Weekly Average',
                        data: new Array(data.length).fill(dailyAvgs.avg_duration),
                        borderColor: '#60a5fa',
                        borderDash: [5, 5],
                        fill: false
                    }
                ]
            }, 'Duration (minutes)');

            createLineChart('dailyWaitChart', {
                labels: data.map(d => d.day),
                datasets: [
                    {
                        label: 'Wait Time',
                        data: data.map(d => d.avg_wait_time),
                        borderColor: '#ec4899',
                        backgroundColor: 'rgba(236, 72, 153, 0.1)',
                        fill: true,
                        tension: 0.4
                    },
                    {
                        label: 'Weekly Average',
                        data: new Array(data.length).fill(dailyAvgs.avg_wait_time),
                        borderColor: '#60a5fa',
                        borderDash: [5, 5],
                        fill: false
                    }
                ]
            }, 'Wait Time (minutes)');
        }

        // Create monthly charts INCLUDING total fare chart
        function createMonthlyCharts(data) {
            const monthlyAvgs = {
                volume: data.length > 0 ? data.reduce((sum, d) => sum + d.volume, 0) / data.length : 0,
                price_per_mile: data.length > 0 ? data.reduce((sum, d) => sum + d.price_per_mile, 0) / data.length : 0,
                total_fare_amount: data.length > 0 ? data.reduce((sum, d) => sum + d.total_fare_amount, 0) / data.length : 0,
                avg_duration: data.length > 0 ? data.reduce((sum, d) => sum + d.avg_duration, 0) / data.length : 0,
                avg_wait_time: data.length > 0 ? data.reduce((sum, d) => sum + d.avg_wait_time, 0) / data.length : 0
            };

            createLineChart('monthlyVolumeChart', {
                labels: data.map(d => d.month_name),
                datasets: [
                    {
                        label: 'Trip Volume',
                        data: data.map(d => d.volume),
                        borderColor: '#ffffff',
                        backgroundColor: 'rgba(255, 255, 255, 0.1)',
                        fill: true,
                        tension: 0.4
                    },
                    {
                        label: 'Yearly Average',
                        data: new Array(data.length).fill(monthlyAvgs.volume),
                        borderColor: '#60a5fa',
                        borderDash: [5, 5],
                        fill: false
                    }
                ]
            }, 'Trip Count');

            createLineChart('monthlyPriceChart', {
                labels: data.map(d => d.month_name),
                datasets: [
                    {
                        label: 'Price per Mile',
                        data: data.map(d => d.price_per_mile),
                        borderColor: '#34d399',
                        backgroundColor: 'rgba(52, 211, 153, 0.1)',
                        fill: true,
                        tension: 0.4
                    },
                    {
                        label: 'Yearly Average',
                        data: new Array(data.length).fill(monthlyAvgs.price_per_mile),
                        borderColor: '#60a5fa',
                        borderDash: [5, 5],
                        fill: false
                    }
                ]
            }, 'Price ($)');

            // NEW: Total Fare Amount Chart for Monthly
            createLineChart('monthlyTotalFareChart', {
                labels: data.map(d => d.month_name),
                datasets: [
                    {
                        label: 'Actual Total Price',
                        data: data.map(d => d.total_fare_amount),
                        borderColor: '#06b6d4',
                        backgroundColor: 'rgba(6, 182, 212, 0.1)',
                        fill: true,
                        tension: 0.4
                    },
                    {
                        label: 'Yearly Average',
                        data: new Array(data.length).fill(monthlyAvgs.total_fare_amount),
                        borderColor: '#60a5fa',
                        borderDash: [5, 5],
                        fill: false
                    }
                ]
            }, 'Price ($)', {
                tooltipCallback: function(context) {
                    const month = context.label;
                    const price = context.parsed.y.toFixed(2);
                    const yearlyAvg = monthlyAvgs.total_fare_amount.toFixed(2);
                    return [
                        `Month: ${month}`,
                        `Total Price: ${price}`,
                        `Yearly Average: ${yearlyAvg}`
                    ];
                }
            });

            createLineChart('monthlyDurationChart', {
                labels: data.map(d => d.month_name),
                datasets: [
                    {
                        label: 'Duration',
                        data: data.map(d => d.avg_duration),
                        borderColor: '#f59e0b',
                        backgroundColor: 'rgba(245, 158, 11, 0.1)',
                        fill: true,
                        tension: 0.4
                    },
                    {
                        label: 'Yearly Average',
                        data: new Array(data.length).fill(monthlyAvgs.avg_duration),
                        borderColor: '#60a5fa',
                        borderDash: [5, 5],
                        fill: false
                    }
                ]
            }, 'Duration (minutes)');

            createLineChart('monthlyWaitChart', {
                labels: data.map(d => d.month_name),
                datasets: [
                    {
                        label: 'Wait Time',
                        data: data.map(d => d.avg_wait_time),
                        borderColor: '#ec4899',
                        backgroundColor: 'rgba(236, 72, 153, 0.1)',
                        fill: true,
                        tension: 0.4
                    },
                    {
                        label: 'Yearly Average',
                        data: new Array(data.length).fill(monthlyAvgs.avg_wait_time),
                        borderColor: '#60a5fa',
                        borderDash: [5, 5],
                        fill: false
                    }
                ]
            }, 'Wait Time (minutes)');
        }

        // Create line chart helper with enhanced tooltip support
        function createLineChart(canvasId, data, yAxisLabel, tooltipOptions = {}) {
            const ctx = document.getElementById(canvasId).getContext('2d');
            
            // Destroy existing chart if it exists
            if (charts[canvasId]) {
                charts[canvasId].destroy();
            }
            
            // Chart.js defaults for dark theme
            Chart.defaults.color = '#9ca3af';
            Chart.defaults.borderColor = '#374151';
            
            charts[canvasId] = new Chart(ctx, {
                type: 'line',
                data: data,
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        legend: {
                            position: 'top',
                            labels: {
                                color: '#9ca3af',
                                usePointStyle: true,
                                padding: 20
                            }
                        },
                        tooltip: {
                            mode: 'index',
                            intersect: false,
                            backgroundColor: '#1f2937',
                            titleColor: '#ffffff',
                            bodyColor: '#9ca3af',
                            borderColor: '#374151',
                            borderWidth: 1,
                            callbacks: tooltipOptions.tooltipCallback ? {
                                afterBody: tooltipOptions.tooltipCallback
                            } : {}
                        }
                    },
                    scales: {
                        x: {
                            title: {
                                display: true,
                                text: 'Time Period',
                                color: '#9ca3af'
                            },
                            ticks: {
                                color: '#9ca3af'
                            },
                            grid: {
                                color: '#374151'
                            }
                        },
                        y: {
                            title: {
                                display: true,
                                text: yAxisLabel,
                                color: '#9ca3af'
                            },
                            beginAtZero: true,
                            ticks: {
                                color: '#9ca3af'
                            },
                            grid: {
                                color: '#374151'
                            }
                        }
                    },
                    interaction: {
                        mode: 'nearest',
                        axis: 'x',
                        intersect: false
                    }
                }
            });
        }

        // Show message helper
        function showMessage(message, type) {
            const errorEl = document.getElementById('errorMsg');
            const successEl = document.getElementById('successMsg');
            const infoEl = document.getElementById('infoMsg');
            
            errorEl.style.display = 'none';
            successEl.style.display = 'none';
            infoEl.style.display = 'none';
            
            if (type === 'error') {
                errorEl.textContent = message;
                errorEl.style.display = 'block';
                setTimeout(() => errorEl.style.display = 'none', 8000);
            } else if (type === 'info') {
                infoEl.textContent = message;
                infoEl.style.display = 'block';
                setTimeout(() => infoEl.style.display = 'none', 4000);
            } else {
                successEl.textContent = message;
                successEl.style.display = 'block';
                setTimeout(() => successEl.style.display = 'none', 5000);
            }
        }

        // Initialize when page loads
        document.addEventListener('DOMContentLoaded', initApp);
    </script>
</body>
</html>
'''

if __name__ == '__main__':
    print("NYC Taxi Analytics Dashboard with Total Fare Tracking and High Impact Routes")
    print("=" * 70)
    print("Initializing database with 2024 FHVHV data...")
    
    if init_database():
        print("Database initialized successfully!")
        print("Starting Flask server...")
        print("Open your browser to: http://localhost:8000")
        print("=" * 70)
        print("New Features Added:")
        print("✅ High Impact Routes by Hour - Top 10 routes for selected hour")
        print("✅ High Impact Routes by Day - Top 10 routes for selected day")
        print("✅ High Impact Routes by Month - Top 10 routes for selected month")
        print("✅ Independent of map selection - works with entire dataset")
        print("=" * 70)
        port = int(os.environ.get('PORT', '8000'))
        app.run(debug=False, host='0.0.0.0', port=port)
    else:
        print("Failed to initialize database. Check your data paths!")
        print(f"FHVHV Path: {FHVHV_PATH}")
        print("Make sure the parquet files exist at this location.")