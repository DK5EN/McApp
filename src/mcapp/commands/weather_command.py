"""WeatherCommandMixin: weather command handler."""

from .constants import has_console


class WeatherCommandMixin:
    """Mixin providing the weather command handler."""

    def _init_weather(self):
        """Initialize weather service. Called from CommandHandler.__init__."""
        from ..meteo import WeatherService

        try:
            self.weather_service = WeatherService(
                self.lat, self.lon, self.stat_name, max_age_minutes=30
            )
            if has_console:
                print("🌤️  CommandHandler: Weather service initialized (location from GPS)")
        except ImportError as e:
            self.weather_service = None
            if has_console:
                print(f"❌ CommandHandler: Weather service unavailable: {e}")

    async def handle_weather(self, kwargs, requester):
        try:
            if has_console:
                print(f"🌤️  CommandHandler: Getting weather data for {requester}")

            weather_data = self.weather_service.get_weather_data()

            if "error" in weather_data:
                if has_console:
                    print(f"❌ Weather error: {weather_data['error']}")
                return f"❌ Weather unavailable: {weather_data['error'][:30]}"

            prefix_text = kwargs.get("text", "")
            weather_msg = self.weather_service.format_for_lora(
                weather_data, prefix_text=prefix_text
            )

            if has_console:
                source = weather_data.get("data_source", "Unknown")
                quality = weather_data.get("data_quality", "Unknown")
                age = weather_data.get("data_age_minutes", 0)
                print(f"✅ Weather delivered: {source}, Quality: {quality}, Age: {age:.1f}min")

                if (
                    "supplemented_parameters" in weather_data
                    and weather_data["supplemented_parameters"]
                ):
                    supplemented = ", ".join(weather_data["supplemented_parameters"])
                    print(f"🔗 Fusion used: {supplemented} from OpenMeteo")

            return weather_msg

        except Exception as e:
            error_msg = f"Weather service error: {str(e)[:40]}"
            if has_console:
                print(f"❌ Weather handler error: {e}")
            return f"❌ {error_msg}"
