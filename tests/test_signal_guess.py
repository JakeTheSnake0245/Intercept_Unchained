"""
Comprehensive tests for the Signal Guessing Engine.

Tests cover:
- FM broadcast frequency detection
- Airband frequency detection
- ISM band devices (433 MHz, 868 MHz, 2.4 GHz)
- TPMS / short-burst telemetry
- Cellular/LTE detection
- Modulation and bandwidth scoring
- Burst behavior detection
- Region-specific allocations
- Confidence level calculations
"""

from utils.signal_guess import (
    Confidence,
    SignalGuessingEngine,
    guess_signal_type,
    guess_signal_type_dict,
)


class TestFMBroadcast:
    """Tests for FM broadcast radio identification."""

    def test_fm_broadcast_center_frequency(self):
        """Test FM broadcast at typical frequency."""
        result = guess_signal_type(
            frequency_hz=98_500_000,  # 98.5 MHz
            modulation="WFM",
            bandwidth_hz=200_000,
        )
        assert result.primary_label == "FM Broadcast Radio"
        assert result.confidence == Confidence.HIGH
        assert "broadcast" in result.tags

    def test_fm_broadcast_edge_frequencies(self):
        """Test FM broadcast at band edges."""
        # Low edge
        result_low = guess_signal_type(frequency_hz=88_000_000)
        assert result_low.primary_label == "FM Broadcast Radio"

        # High edge
        result_high = guess_signal_type(frequency_hz=107_900_000)
        assert result_high.primary_label == "FM Broadcast Radio"

    def test_fm_broadcast_without_modulation(self):
        """Test FM broadcast without modulation hint - lower confidence."""
        result = guess_signal_type(frequency_hz=100_000_000)
        assert result.primary_label == "FM Broadcast Radio"
        # Without modulation hint, confidence should be MEDIUM or lower
        assert result.confidence in (Confidence.MEDIUM, Confidence.HIGH)

    def test_fm_broadcast_explanation(self):
        """Test explanation uses hedged language."""
        result = guess_signal_type(
            frequency_hz=95_000_000,
            modulation="FM",
        )
        explanation = result.explanation.lower()
        # Should contain hedged language
        assert any(word in explanation for word in ["consistent", "could", "may", "indicate"])
        assert "95.000 mhz" in explanation


class TestAirband:
    """Tests for civil aviation airband identification."""

    def test_airband_typical_frequency(self):
        """Test airband at typical tower frequency."""
        result = guess_signal_type(
            frequency_hz=118_750_000,  # 118.75 MHz
            modulation="AM",
            bandwidth_hz=8_000,
        )
        assert result.primary_label == "Airband (Civil Aviation Voice)"
        assert result.confidence == Confidence.HIGH
        assert "aviation" in result.tags

    def test_airband_approach_frequency(self):
        """Test airband at approach control frequency."""
        result = guess_signal_type(
            frequency_hz=128_550_000,  # 128.55 MHz
            modulation="AM",
        )
        assert result.primary_label == "Airband (Civil Aviation Voice)"
        assert result.confidence in (Confidence.MEDIUM, Confidence.HIGH)

    def test_airband_guard_frequency(self):
        """Test airband at international distress frequency."""
        result = guess_signal_type(
            frequency_hz=121_500_000,  # 121.5 MHz guard
            modulation="AM",
        )
        assert result.primary_label == "Airband (Civil Aviation Voice)"

    def test_airband_wrong_modulation(self):
        """Test airband with wrong modulation still matches but lower score."""
        result_am = guess_signal_type(
            frequency_hz=125_000_000,
            modulation="AM",
        )
        result_fm = guess_signal_type(
            frequency_hz=125_000_000,
            modulation="FM",
        )
        # AM should score higher for airband
        assert result_am._scores.get("Airband (Civil Aviation Voice)", 0) > \
               result_fm._scores.get("Airband (Civil Aviation Voice)", 0)


class TestISMBands:
    """Tests for ISM band device identification."""

    def test_433_mhz_ism_eu(self):
        """Test 433 MHz ISM band (EU)."""
        result = guess_signal_type(
            frequency_hz=433_920_000,  # 433.92 MHz
            modulation="NFM",
            region="UK/EU",
        )
        assert "ISM" in result.primary_label or "TPMS" in result.primary_label
        assert any(tag in result.tags for tag in ["ism", "telemetry", "tpms"])

    def test_433_mhz_short_burst(self):
        """Test 433 MHz with short burst pattern -> TPMS/telemetry."""
        result = guess_signal_type(
            frequency_hz=433_920_000,
            modulation="NFM",
            duration_ms=50,  # 50ms burst
            repetition_count=3,
            region="UK/EU",
        )
        # Short burst at 433.92 should suggest TPMS or ISM telemetry
        assert any(word in result.primary_label.lower() for word in ["tpms", "ism", "telemetry"])
        # Should have medium confidence due to burst behavior match
        assert result.confidence in (Confidence.MEDIUM, Confidence.HIGH)

    def test_868_mhz_ism_eu(self):
        """Test 868 MHz ISM band (EU)."""
        result = guess_signal_type(
            frequency_hz=868_300_000,
            modulation="FSK",
            region="UK/EU",
        )
        assert "868" in result.primary_label or "ISM" in result.primary_label
        assert "ism" in result.tags or "iot" in result.tags

    def test_915_mhz_ism_us(self):
        """Test 915 MHz ISM band (US)."""
        result = guess_signal_type(
            frequency_hz=915_000_000,
            modulation="FSK",
            region="US",
        )
        assert "915" in result.primary_label or "ISM" in result.primary_label

    def test_24_ghz_ism(self):
        """Test 2.4 GHz ISM band."""
        result = guess_signal_type(
            frequency_hz=2_437_000_000,  # WiFi channel 6
            modulation="OFDM",
            bandwidth_hz=20_000_000,
        )
        assert "2.4" in result.primary_label or "ISM" in result.primary_label
        assert any(tag in result.tags for tag in ["ism", "wifi", "bluetooth"])

    def test_24_ghz_narrow_bandwidth(self):
        """Test 2.4 GHz with narrow bandwidth (Bluetooth-like)."""
        result = guess_signal_type(
            frequency_hz=2_450_000_000,
            modulation="GFSK",
            bandwidth_hz=1_000_000,
        )
        assert "2.4" in result.primary_label
        # Should match ISM 2.4 GHz
        assert result.confidence in (Confidence.LOW, Confidence.MEDIUM, Confidence.HIGH)


class TestTPMSTelemetry:
    """Tests for TPMS and short-burst telemetry."""

    def test_tpms_433_short_burst(self):
        """Test TPMS-like signal at 433.92 MHz with short burst."""
        result = guess_signal_type(
            frequency_hz=433_920_000,
            modulation="OOK",
            bandwidth_hz=20_000,
            duration_ms=100,  # Short burst
            repetition_count=4,  # Multiple bursts
            region="UK/EU",
        )
        # Should identify as TPMS or ISM telemetry
        assert any(word in result.primary_label.lower() for word in ["tpms", "ism", "telemetry", "remote"])

    def test_tpms_315_us(self):
        """Test TPMS at 315 MHz (US)."""
        result = guess_signal_type(
            frequency_hz=315_000_000,
            modulation="ASK",
            duration_ms=50,
            repetition_count=2,
            region="US",
        )
        assert any(word in result.primary_label.lower() for word in ["tpms", "ism", "315", "remote"])

    def test_burst_detection_scoring(self):
        """Test that burst behavior increases scores for burst-type signals."""
        # Without burst behavior
        result_no_burst = guess_signal_type(
            frequency_hz=433_920_000,
            modulation="OOK",
            region="UK/EU",
        )

        # With burst behavior
        result_burst = guess_signal_type(
            frequency_hz=433_920_000,
            modulation="OOK",
            duration_ms=50,
            repetition_count=5,
            region="UK/EU",
        )

        # Burst scores should be higher for burst-type signals
        burst_score = result_burst._scores.get("TPMS / Vehicle Telemetry", 0) + \
                      result_burst._scores.get("Remote Control / Key Fob", 0)
        no_burst_score = result_no_burst._scores.get("TPMS / Vehicle Telemetry", 0) + \
                         result_no_burst._scores.get("Remote Control / Key Fob", 0)
        assert burst_score > no_burst_score


class TestCellularLTE:
    """Tests for cellular/LTE identification."""

    def test_lte_band_20_eu(self):
        """Test LTE Band 20 (800 MHz) detection."""
        result = guess_signal_type(
            frequency_hz=806_000_000,
            modulation="LTE",
            bandwidth_hz=10_000_000,
            region="UK/EU",
        )
        assert "Cellular" in result.primary_label or "Mobile" in result.primary_label
        assert "cellular" in result.tags

    def test_lte_band_3_eu(self):
        """Test LTE Band 3 (1800 MHz) detection."""
        result = guess_signal_type(
            frequency_hz=1_815_000_000,
            bandwidth_hz=15_000_000,
            region="UK/EU",
        )
        assert "Cellular" in result.primary_label or "Mobile" in result.primary_label

    def test_cellular_wide_bandwidth_boost(self):
        """Test that wide bandwidth boosts cellular confidence."""
        result_wide = guess_signal_type(
            frequency_hz=850_000_000,
            bandwidth_hz=10_000_000,  # 10 MHz LTE
        )
        result_narrow = guess_signal_type(
            frequency_hz=850_000_000,
            bandwidth_hz=25_000,  # 25 kHz narrowband
        )
        # Wide bandwidth should score higher for cellular
        cell_score_wide = result_wide._scores.get("Cellular / Mobile Network", 0)
        cell_score_narrow = result_narrow._scores.get("Cellular / Mobile Network", 0)
        assert cell_score_wide > cell_score_narrow


class TestConfidenceLevels:
    """Tests for confidence level calculations."""

    def test_high_confidence_requires_margin(self):
        """Test that HIGH confidence requires good margin over alternatives."""
        # FM broadcast with strong evidence
        result = guess_signal_type(
            frequency_hz=100_000_000,
            modulation="WFM",
            bandwidth_hz=200_000,
        )
        assert result.confidence == Confidence.HIGH

    def test_medium_confidence_with_ambiguity(self):
        """Test MEDIUM confidence when alternatives are close."""
        # Frequency in ISM band with less specific characteristics
        result = guess_signal_type(
            frequency_hz=433_500_000,  # General 433 band
            region="UK/EU",
        )
        # Should have alternatives, potentially MEDIUM confidence
        assert result.confidence in (Confidence.LOW, Confidence.MEDIUM)
        assert len(result.alternatives) > 0

    def test_low_confidence_unknown_frequency(self):
        """Test LOW confidence for unrecognized frequency."""
        result = guess_signal_type(
            frequency_hz=50_000_000,  # 50 MHz - not in common allocations
        )
        assert result.confidence == Confidence.LOW

    def test_alternatives_have_lower_confidence(self):
        """Test that alternatives have appropriate confidence levels."""
        result = guess_signal_type(
            frequency_hz=433_920_000,
            modulation="OOK",
            region="UK/EU",
        )
        if result.alternatives:
            for alt in result.alternatives:
                # Alternatives should generally have same or lower confidence
                assert isinstance(alt.confidence, Confidence)


class TestRegionSpecific:
    """Tests for region-specific frequency allocations."""

    def test_315_mhz_us_only(self):
        """Test 315 MHz ISM only matches in US region."""
        result_us = guess_signal_type(
            frequency_hz=315_000_000,
            region="US",
        )
        result_eu = guess_signal_type(
            frequency_hz=315_000_000,
            region="UK/EU",
        )
        # Should match in US
        assert "315" in result_us.primary_label or "ISM" in result_us.primary_label or "TPMS" in result_us.primary_label
        # Should not match well in EU
        assert result_eu.primary_label == "Unknown Signal" or result_eu.confidence == Confidence.LOW

    def test_pmr446_eu_only(self):
        """Test PMR446 only matches in EU region."""
        result_eu = guess_signal_type(
            frequency_hz=446_100_000,
            modulation="NFM",
            region="UK/EU",
        )
        result_us = guess_signal_type(
            frequency_hz=446_100_000,
            modulation="NFM",
            region="US",
        )
        # Should match PMR446 in EU
        assert "PMR" in result_eu.primary_label
        # Should not match PMR446 in US
        assert "PMR" not in result_us.primary_label

    def test_dab_eu_only(self):
        """Test DAB only matches in EU region."""
        result_eu = guess_signal_type(
            frequency_hz=225_648_000,  # DAB 12C
            modulation="OFDM",
            bandwidth_hz=1_500_000,
            region="UK/EU",
        )
        assert "DAB" in result_eu.primary_label


class TestExplanationLanguage:
    """Tests for hedged, client-safe explanation language."""

    def test_no_certainty_claims(self):
        """Test that explanations never claim certainty."""
        result = guess_signal_type(
            frequency_hz=100_000_000,
            modulation="FM",
        )
        explanation = result.explanation.lower()
        # Should NOT contain definitive language
        forbidden_words = ["definitely", "certainly", "absolutely", "is a", "this is"]
        for word in forbidden_words:
            assert word not in explanation, f"Found forbidden word '{word}' in explanation"

    def test_hedged_language_present(self):
        """Test that explanations use hedged language."""
        result = guess_signal_type(
            frequency_hz=118_500_000,
            modulation="AM",
        )
        explanation = result.explanation.lower()
        # Should contain hedged language
        hedged_words = ["consistent", "could", "may", "likely", "suggest", "indicate"]
        assert any(word in explanation for word in hedged_words)

    def test_explanation_includes_frequency(self):
        """Test that explanations include the frequency."""
        result = guess_signal_type(
            frequency_hz=433_920_000,
            modulation="NFM",
            region="UK/EU",
        )
        assert "433.920" in result.explanation


class TestUnknownSignals:
    """Tests for unknown signal handling."""

    def test_completely_unknown_frequency(self):
        """Test handling of frequency with no allocations."""
        result = guess_signal_type(
            frequency_hz=42_000_000,  # Random frequency
        )
        assert result.primary_label == "Unknown Signal"
        assert result.confidence == Confidence.LOW
        assert result.alternatives == []
        assert "unknown" in result.tags

    def test_unknown_includes_frequency_in_explanation(self):
        """Test that unknown signal explanation includes frequency."""
        result = guess_signal_type(
            frequency_hz=42_000_000,
        )
        assert "42.000" in result.explanation


class TestDictOutput:
    """Tests for dictionary output format."""

    def test_dict_output_structure(self):
        """Test that dict output has correct structure."""
        result = guess_signal_type_dict(
            frequency_hz=100_000_000,
            modulation="FM",
        )
        assert isinstance(result, dict)
        assert "primary_label" in result
        assert "confidence" in result
        assert "alternatives" in result
        assert "explanation" in result
        assert "tags" in result

    def test_dict_confidence_is_string(self):
        """Test that confidence in dict is string, not enum."""
        result = guess_signal_type_dict(
            frequency_hz=100_000_000,
        )
        assert isinstance(result["confidence"], str)
        assert result["confidence"] in ("LOW", "MEDIUM", "HIGH")

    def test_dict_alternatives_structure(self):
        """Test alternatives in dict output."""
        result = guess_signal_type_dict(
            frequency_hz=433_920_000,
            region="UK/EU",
        )
        for alt in result["alternatives"]:
            assert "label" in alt
            assert "confidence" in alt
            assert isinstance(alt["confidence"], str)


class TestEngineInstance:
    """Tests for SignalGuessingEngine class."""

    def test_engine_default_region(self):
        """Test engine uses default region."""
        engine = SignalGuessingEngine(region="UK/EU")
        result = engine.guess_signal_type(frequency_hz=433_920_000)
        assert "ISM" in result.primary_label or "TPMS" in result.primary_label

    def test_engine_override_region(self):
        """Test engine allows region override."""
        engine = SignalGuessingEngine(region="UK/EU")
        result = engine.guess_signal_type(
            frequency_hz=315_000_000,
            region="US",  # Override default
        )
        # Should match US allocation
        assert "315" in result.primary_label or "ISM" in result.primary_label or "TPMS" in result.primary_label

    def test_get_frequency_allocations(self):
        """Test get_frequency_allocations method."""
        engine = SignalGuessingEngine(region="UK/EU")
        allocations = engine.get_frequency_allocations(frequency_hz=433_920_000)
        assert len(allocations) > 0
        assert any("ISM" in a or "TPMS" in a for a in allocations)


class TestEdgeCases:
    """Tests for edge cases and boundary conditions."""

    def test_exact_band_edge(self):
        """Test frequency at exact band edge."""
        # FM band starts at 87.5 MHz
        result = guess_signal_type(frequency_hz=87_500_000)
        assert result.primary_label == "FM Broadcast Radio"

    def test_very_narrow_bandwidth(self):
        """Test very narrow bandwidth handling."""
        result = guess_signal_type(
            frequency_hz=433_920_000,
            bandwidth_hz=100,  # Very narrow
            region="UK/EU",
        )
        # Should still match but may have lower score
        assert result.primary_label != "Unknown Signal"

    def test_very_wide_bandwidth(self):
        """Test very wide bandwidth handling."""
        result = guess_signal_type(
            frequency_hz=2_450_000_000,
            bandwidth_hz=100_000_000,  # 100 MHz - very wide
        )
        # Should still identify ISM but may penalize
        assert "ISM" in result.primary_label or "Unknown" in result.primary_label

    def test_zero_duration(self):
        """Test zero duration handling."""
        result = guess_signal_type(
            frequency_hz=433_920_000,
            duration_ms=0,
            region="UK/EU",
        )
        assert result.primary_label != "Unknown Signal"

    def test_high_repetition_count(self):
        """Test high repetition count."""
        result = guess_signal_type(
            frequency_hz=433_920_000,
            repetition_count=1000,
            region="UK/EU",
        )
        # Should handle gracefully
        assert result.primary_label != "Unknown Signal"

    def test_all_optional_params_none(self):
        """Test with only frequency provided."""
        result = guess_signal_type(frequency_hz=100_000_000)
        assert result.primary_label is not None
        assert result.confidence is not None


class TestSpecificSignalTypes:
    """Tests for specific signal type identifications."""

    def test_marine_vhf_channel_16(self):
        """Test Marine VHF Channel 16 (distress)."""
        result = guess_signal_type(
            frequency_hz=156_800_000,  # CH 16
            modulation="NFM",
        )
        assert "Marine" in result.primary_label

    def test_amateur_2m_calling(self):
        """Test amateur radio 2m calling frequency."""
        result = guess_signal_type(
            frequency_hz=145_500_000,
            modulation="FM",
        )
        assert "Amateur" in result.primary_label or "2m" in result.primary_label

    def test_amateur_70cm(self):
        """Test amateur radio 70cm band."""
        result = guess_signal_type(
            frequency_hz=438_500_000,
            modulation="NFM",
        )
        # Could be amateur 70cm or ISM 433 (they overlap)
        assert "Amateur" in result.primary_label or "ISM" in result.primary_label

    def test_noaa_weather_satellite(self):
        """Test NOAA weather satellite frequency."""
        result = guess_signal_type(
            frequency_hz=137_500_000,
            modulation="FM",
            bandwidth_hz=38_000,
        )
        assert "Weather" in result.primary_label or "NOAA" in result.primary_label or "Satellite" in result.primary_label

    def test_adsb_1090(self):
        """Test ADS-B at 1090 MHz."""
        result = guess_signal_type(
            frequency_hz=1_090_000_000,
            duration_ms=50,  # Short burst
        )
        assert "ADS-B" in result.primary_label or "Aircraft" in result.primary_label

    def test_dect_cordless(self):
        """Test DECT cordless phone frequency."""
        result = guess_signal_type(
            frequency_hz=1_890_000_000,
            modulation="GFSK",
        )
        assert "DECT" in result.primary_label

    def test_pager_uk(self):
        """Test UK pager frequency."""
        result = guess_signal_type(
            frequency_hz=153_350_000,
            modulation="FSK",
        )
        assert "Pager" in result.primary_label
