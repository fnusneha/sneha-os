# Keep the model DTOs (reflected on by Moshi).
-keep class os.sneha.data.** { *; }
-keepclassmembers class kotlin.Metadata { *; }
# OkHttp / Moshi platform metadata
-dontwarn okhttp3.**
-dontwarn okio.**
