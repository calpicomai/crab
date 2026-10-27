# Unix seconds; zero/empty values without validity are unknown placeholders.
# Own bounds map Pi time to bridge time. Reported monotonic time is metadata only.
uint32 schema_version
string<=64 producer
string<=64 robot_boot_id
bool session_id_valid
string<=64 session_id
bool publisher_id_valid
string<=64 publisher_id
bool publisher_started_at_ns_valid
uint64 publisher_started_at_ns
bool sequence_valid
uint64 sequence
bool captured_at_valid
float64 captured_at
string<=32 captured_clock_domain
bool producer_received_at_valid
float64 producer_received_at
string<=32 producer_received_clock_domain
float64 bridge_received_at
# All three monotonic fields belong only to this bridge process.
float64 bridge_received_mono
float64 age_evaluated_mono
float64 calibration_valid_until_mono
bool offset_bounds_valid
float64 offset_lower_s
float64 offset_upper_s
bool age_upper_valid
float64 age_upper_s
bool timing_verified
bool fresh
bool reported_clock_valid
float64 reported_offset_lower_s
float64 reported_offset_upper_s
float64 reported_wall_at
float64 reported_mono_at
bool reported_produced_at_pi_lower_valid
float64 reported_produced_at_pi_lower
