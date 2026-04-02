"""
Media file utilities for handling uploads, processing, and management.
This module provides functionality for image and video processing.
"""

import os
import io
from typing import Optional, Dict, Any, BinaryIO, Tuple
from PIL import Image, ImageOps
import magic
from datetime import datetime

# Import with fallback for different execution contexts
try:
    from ..log_config import error
except ImportError:
    from log_config import error

class MediaProcessor:
    """Media file processing utilities."""
    
    # Supported file types
    SUPPORTED_IMAGE_TYPES = {
        'image/jpeg': '.jpg',
        'image/png': '.png',
        'image/gif': '.gif',
        'image/webp': '.webp'
    }
    
    SUPPORTED_VIDEO_TYPES = {
        'video/mp4': '.mp4',
        'video/mpeg': '.mpeg',
        'video/quicktime': '.mov',
        'video/x-msvideo': '.avi',
        'video/webm': '.webm'
    }
    
    # File size limits (in bytes)
    MAX_IMAGE_SIZE = 10 * 1024 * 1024  # 10MB
    MAX_VIDEO_SIZE = 100 * 1024 * 1024  # 100MB
    
    # Image processing settings
    THUMBNAIL_SIZE = (300, 300)
    MAX_IMAGE_DIMENSION = 2048
    
    @classmethod
    def validate_file(cls, file_data: BinaryIO, filename: str) -> Dict[str, Any]:
        """
        Validate uploaded file.
        
        Args:
            file_data: Binary file data
            filename: Original filename
            
        Returns:
            Dict containing validation results
        """
        try:
            # Reset file pointer
            file_data.seek(0)
            
            # Get file size
            file_data.seek(0, 2)  # Seek to end
            file_size = file_data.tell()
            file_data.seek(0)  # Reset to beginning
            
            # Detect MIME type
            file_content = file_data.read(1024)  # Read first 1KB for detection
            file_data.seek(0)  # Reset to beginning
            
            mime_type = magic.from_buffer(file_content, mime=True)
            
            # Determine file type
            file_type = None
            if mime_type in cls.SUPPORTED_IMAGE_TYPES:
                file_type = 'image'
                max_size = cls.MAX_IMAGE_SIZE
            elif mime_type in cls.SUPPORTED_VIDEO_TYPES:
                file_type = 'video'
                max_size = cls.MAX_VIDEO_SIZE
            else:
                return {
                    'valid': False,
                    'error': f'Unsupported file type: {mime_type}',
                    'mime_type': mime_type
                }
            
            # Check file size
            if file_size > max_size:
                return {
                    'valid': False,
                    'error': f'File too large. Maximum size for {file_type}: {max_size / (1024*1024):.1f}MB',
                    'file_size': file_size,
                    'mime_type': mime_type
                }
            
            return {
                'valid': True,
                'file_type': file_type,
                'mime_type': mime_type,
                'file_size': file_size,
                'file_extension': cls.SUPPORTED_IMAGE_TYPES.get(mime_type) or cls.SUPPORTED_VIDEO_TYPES.get(mime_type)
            }
            
        except Exception as e:
            error(f"Error validating file {filename}: {e}")
            return {
                'valid': False,
                'error': f'File validation error: {str(e)}'
            }
    
    @classmethod
    def process_image(cls, image_data: BinaryIO, filename: str) -> Dict[str, Any]:
        """
        Process uploaded image (resize, optimize, create thumbnail).
        
        Args:
            image_data: Binary image data
            filename: Original filename
            
        Returns:
            Dict containing processed image data
        """
        try:
            # Reset file pointer
            image_data.seek(0)
            
            # Open image with PIL
            with Image.open(image_data) as img:
                # Convert to RGB if necessary (for JPEG compatibility)
                if img.mode in ('RGBA', 'LA', 'P'):
                    # Create white background for transparency
                    background = Image.new('RGB', img.size, (255, 255, 255))
                    if img.mode == 'P':
                        img = img.convert('RGBA')
                    background.paste(img, mask=img.split()[-1] if img.mode == 'RGBA' else None)
                    img = background
                elif img.mode != 'RGB':
                    img = img.convert('RGB')
                
                # Auto-orient image based on EXIF data
                img = ImageOps.exif_transpose(img)
                
                # Resize if too large
                original_size = img.size
                if max(img.size) > cls.MAX_IMAGE_DIMENSION:
                    img.thumbnail((cls.MAX_IMAGE_DIMENSION, cls.MAX_IMAGE_DIMENSION), Image.Resampling.LANCZOS)
                
                # Create main image buffer
                main_buffer = io.BytesIO()
                img.save(main_buffer, format='JPEG', quality=85, optimize=True)
                main_buffer.seek(0)
                
                # Create thumbnail
                thumbnail_img = img.copy()
                thumbnail_img.thumbnail(cls.THUMBNAIL_SIZE, Image.Resampling.LANCZOS)
                
                thumbnail_buffer = io.BytesIO()
                thumbnail_img.save(thumbnail_buffer, format='JPEG', quality=80, optimize=True)
                thumbnail_buffer.seek(0)
                
                return {
                    'success': True,
                    'main_image': main_buffer,
                    'thumbnail': thumbnail_buffer,
                    'original_size': original_size,
                    'processed_size': img.size,
                    'thumbnail_size': thumbnail_img.size
                }
                
        except Exception as e:
            error(f"Error processing image {filename}: {e}")
            return {
                'success': False,
                'error': f'Image processing error: {str(e)}'
            }
    
    @classmethod
    def extract_video_metadata(cls, video_data: BinaryIO, filename: str) -> Dict[str, Any]:
        """
        Extract metadata from video file.
        
        Args:
            video_data: Binary video data
            filename: Original filename
            
        Returns:
            Dict containing video metadata
        """
        try:
            # For basic implementation, we'll return file size and type
            # In production, you might want to use ffmpeg-python for detailed metadata
            video_data.seek(0, 2)
            file_size = video_data.tell()
            video_data.seek(0)
            
            return {
                'success': True,
                'file_size': file_size,
                'duration': None,  # Would need ffmpeg for this
                'resolution': None,  # Would need ffmpeg for this
                'bitrate': None  # Would need ffmpeg for this
            }
            
        except Exception as e:
            error(f"Error extracting video metadata from {filename}: {e}")
            return {
                'success': False,
                'error': f'Video metadata extraction error: {str(e)}'
            }
    
    @classmethod
    def generate_filename(cls, original_filename: str, user_id: str, file_type: str) -> str:
        """
        Generate a unique filename for storage.
        
        Args:
            original_filename: Original filename
            user_id: User ID
            file_type: Type of file ('image' or 'video')
            
        Returns:
            str: Generated filename
        """
        timestamp = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
        file_extension = os.path.splitext(original_filename)[1].lower()
        
        return f"{file_type}_{user_id}_{timestamp}{file_extension}"

class MediaValidator:
    """Media file validation utilities."""
    
    @staticmethod
    def is_safe_filename(filename: str) -> bool:
        """Check if filename is safe for storage."""
        # Remove path components
        filename = os.path.basename(filename)
        
        # Check for dangerous characters
        dangerous_chars = ['..', '/', '\\', ':', '*', '?', '"', '<', '>', '|']
        for char in dangerous_chars:
            if char in filename:
                return False
        
        # Check length
        if len(filename) > 255:
            return False
        
        return True
    
    @staticmethod
    def sanitize_filename(filename: str) -> str:
        """Sanitize filename for safe storage."""
        # Get basename to remove path components
        filename = os.path.basename(filename)
        
        # Replace dangerous characters
        dangerous_chars = ['/', '\\', ':', '*', '?', '"', '<', '>', '|']
        for char in dangerous_chars:
            filename = filename.replace(char, '_')
        
        # Remove multiple dots (except the last one for extension)
        parts = filename.split('.')
        if len(parts) > 2:
            filename = '_'.join(parts[:-1]) + '.' + parts[-1]
        
        # Limit length
        if len(filename) > 255:
            name, ext = os.path.splitext(filename)
            filename = name[:255-len(ext)] + ext
        
        return filename

class MediaMetadataExtractor:
    """Extract metadata from media files."""
    
    @staticmethod
    def extract_image_metadata(image_data: BinaryIO) -> Dict[str, Any]:
        """Extract metadata from image file."""
        try:
            image_data.seek(0)
            
            with Image.open(image_data) as img:
                metadata = {
                    'format': img.format,
                    'mode': img.mode,
                    'size': img.size,
                    'has_transparency': img.mode in ('RGBA', 'LA', 'P')
                }
                
                # Extract EXIF data if available
                if hasattr(img, '_getexif') and img._getexif():
                    exif_data = img._getexif()
                    if exif_data:
                        # Extract common EXIF tags
                        metadata['exif'] = {
                            'datetime': exif_data.get(306),  # DateTime
                            'camera_make': exif_data.get(271),  # Make
                            'camera_model': exif_data.get(272),  # Model
                            'orientation': exif_data.get(274),  # Orientation
                        }
                
                return metadata
                
        except Exception as e:
            error(f"Error extracting image metadata: {e}")
            return {}
    
    @staticmethod
    def extract_location_from_exif(image_data: BinaryIO) -> Optional[Dict[str, float]]:
        """Extract GPS location from image EXIF data."""
        try:
            image_data.seek(0)
            
            with Image.open(image_data) as img:
                if hasattr(img, '_getexif') and img._getexif():
                    exif_data = img._getexif()
                    if exif_data and 34853 in exif_data:  # GPS Info tag
                        gps_info = exif_data[34853]
                        
                        def convert_to_degrees(value):
                            """Convert GPS coordinates to degrees."""
                            d, m, s = value
                            return d + (m / 60.0) + (s / 3600.0)
                        
                        if 2 in gps_info and 4 in gps_info:  # Latitude and Longitude
                            lat = convert_to_degrees(gps_info[2])
                            lon = convert_to_degrees(gps_info[4])
                            
                            # Check for hemisphere
                            if gps_info.get(1) == 'S':
                                lat = -lat
                            if gps_info.get(3) == 'W':
                                lon = -lon
                            
                            return {'latitude': lat, 'longitude': lon}
                
        except Exception as e:
            error(f"Error extracting GPS location: {e}")
        
        return None