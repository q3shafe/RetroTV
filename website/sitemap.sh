#!/bin/bash
# Sitemap Generator Script

# Configuration (can be set via command line arguments)
url="${1:-https://retrotv.n5net.com/}"
sitemap_file="${2:-sitemap.xml}"
crawl_depth="${3:-1}"
temp_dir="temp_sitemap_$(date +%s)"
temp_links="$temp_dir/links.txt"
log_file="$temp_dir/wget.log"

# Create temp directory
mkdir -p "$temp_dir"

echo "Starting sitemap generation for $url (depth: $crawl_depth)..."

# Step 1: Download website content
# CHANGES: 
# 1. Removed --quiet (fixes empty log file issue)
# 2. Changed error handling below to check for files instead of exit codes
echo "Downloading website content..."
wget --recursive \
     --no-parent \
     --adjust-extension \
     --reject "*.pdf,*.jpg,*.jpeg,*.png,*.gif,*.css,*.js" \
     --level="$crawl_depth" \
     --no-check-certificate \
     --output-file="$log_file" \
     --directory-prefix="$temp_dir" \
     --timeout=10 \
     --tries=3 \
     -e robots=off \
     --user-agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36" \
     "$url"

# NEW LOGIC: Check if files were actually downloaded instead of relying on exit code
# wget returns non-zero for minor issues (like 404s), but we only care if we got content.
file_count=$(find "$temp_dir" -type f \( -name "*.html" -o -name "*.php" -o -name "*.htm" \) | wc -l)

if [ "$file_count" -eq 0 ]; then
    echo "Error: No HTML files were downloaded. Check $log_file for details."
    # Attempt to show the log tail if it exists and is not empty
    if [ -s "$log_file" ]; then
        echo "--- Last 10 lines of wget log ---"
        tail -n 10 "$log_file"
        echo "---------------------------------"
    fi
    exit 1
fi

echo "Download complete. Found $file_count potential HTML files."

# Step 2: Extract valid links with improved pattern matching
echo "Extracting valid links..."
# Find HTML files (wget adds .html to clean URLs thanks to --adjust-extension)
# Then extract hrefs, filtering out common media/assets but allowing clean URLs
find "$temp_dir" -type f -name "*.html" -o -name "*.php" -o -name "*.htm" | \
  xargs grep -l "<html" | \
  xargs grep -Eroh 'href="[^"#?]+"' | \
  sed 's/href="//;s/"$//' | \
  grep -v -E '(javascript:|mailto:|tel:)' | \
  grep -v -E '\.(jpg|jpeg|png|gif|css|js|pdf|ico|svg)(\?|$)' | \
  sort -u > "$temp_links"

# Process links to ensure proper formatting
echo "Processing links..."
processed_links="$temp_dir/processed_links.txt"
> "$processed_links"

# Extract domain for comparison
domain=$(echo "$url" | awk -F/ '{print $3}')

while IFS= read -r link; do
    # Handle relative and absolute URLs properly
    if [[ "$link" == /* ]]; then
        # Absolute path without domain
        echo "${url%/}${link}" >> "$processed_links"
    elif [[ "$link" =~ ^https?:// ]]; then
        # Full URL - only include if from same domain
        if [[ "$link" == *"$domain"* ]]; then
            echo "$link" >> "$processed_links"
        fi
    elif [[ "$link" != "" ]]; then
        # Relative path
        if [[ "${url%/}" == */ ]]; then
            echo "${url%/}$link" >> "$processed_links"
        else
            echo "${url%/}/$link" >> "$processed_links"
        fi
    fi
done < "$temp_links"

# Check if valid links were found
if [ ! -s "$processed_links" ]; then
    echo "No valid links found. Exiting."
    rm -rf "$temp_dir"
    exit 1
fi

# Remove duplicates and normalize URLs
sort -u "$processed_links" | \
  sed 's/\/$//' | \
  sed 's/index\.html$//' | \
  sed 's/index\.php$//' > "$temp_links"

# Get the count of pages for the sitemap
link_count=$(wc -l < "$temp_links")
echo "Found $link_count valid pages to include in the sitemap."

# Step 3: Create the XML file with proper formatting
echo "Generating $sitemap_file..."
cat <<EOF > "$sitemap_file"
<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
EOF

# Get current date in ISO 8601 format
current_date=$(date -u +"%Y-%m-%dT%H:%M:%S+00:00")

# Step 4: Add extracted links to the sitemap with priority calculation
echo "Adding links to $sitemap_file..."
while IFS= read -r full_link; do
    # Calculate priority based on path depth (fewer slashes = higher priority)
    slash_count=$(echo "$full_link" | tr -cd '/' | wc -c)
    base_domain_slashes=$(echo "$url" | tr -cd '/' | wc -c)
    path_depth=$((slash_count - base_domain_slashes))

    # Higher priority for homepage and top-level pages
    if [ "$path_depth" -le 1 ]; then
        priority="1.0"
    elif [ "$path_depth" -eq 2 ]; then
        priority="0.8"
    elif [ "$path_depth" -eq 3 ]; then
        priority="0.6"
    else
        priority="0.4"
    fi

    # Set change frequency based on path depth
    if [ "$path_depth" -le 1 ]; then
        changefreq="daily"
    elif [ "$path_depth" -eq 2 ]; then
        changefreq="weekly"
    else
        changefreq="monthly"
    fi

    cat <<EOF >> "$sitemap_file"
  <url>
    <loc>$full_link</loc>
    <lastmod>$current_date</lastmod>
    <changefreq>$changefreq</changefreq>
    <priority>$priority</priority>
  </url>
EOF
done < "$temp_links"

# Close the XML file
echo "</urlset>" >> "$sitemap_file"

# Validate the XML file
if command -v xmllint &> /dev/null; then
    echo "Validating sitemap XML..."
    xmllint --noout "$sitemap_file"
    if [ $? -eq 0 ]; then
        echo "XML validation successful."
    else
        echo "Warning: XML validation failed. Sitemap may contain errors."
    fi
else
    echo "Note: xmllint not found. Skipping XML validation."
fi

# Check file size - sitemaps should be under 50MB and 50,000 URLs
sitemap_size=$(du -k "$sitemap_file" | cut -f1)
if [ "$sitemap_size" -gt 50000 ]; then
    echo "Warning: Sitemap exceeds 50MB. Consider splitting into multiple sitemaps."
fi

if [ "$link_count" -gt 50000 ]; then
    echo "Warning: Sitemap contains more than 50,000 URLs. Consider creating a sitemap index."
fi

# Step 5: Clean up temporary files
echo "Cleaning up temporary files..."
rm -rf "$temp_dir"

# Calculate execution time if possible
if [ -n "$SECONDS" ]; then
    echo "Sitemap generation completed in $SECONDS seconds."
fi

# Final success message
echo "Sitemap successfully created: $sitemap_file with $link_count URLs"
echo "Don't forget to submit your sitemap to search engines!"
