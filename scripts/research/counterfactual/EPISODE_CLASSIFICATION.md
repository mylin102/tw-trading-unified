# Episode Classification Reference

## Definition

Episode = continuous period where spread Z-score exceeds entry threshold
without returning to the reset band. Defined by market signal reset,
not by trade closure or elapsed time.

## State Machine

```text
                    z >= +2.5
   NEUTRAL ----------------------> WIDE_ACTIVE
       ^                               |
       |                               |
       +------- abs(z) <= 1.0 ---------+

                    z <= -2.5
   NEUTRAL ----------------------> NARROW_ACTIVE
       ^                               |
       |                               |
       +------- abs(z) <= 1.0 ---------+
```

## Thresholds

| Parameter      | Value | Purpose                                    |
|----------------|-------|--------------------------------------------|
| entry_threshold| 2.5   | Enter episode when abs(z) crosses this     |
| reset_z        | 1.0   | Exit episode when abs(z) drops below this  |

## Episode-to-Trade Relationship

```text
Market Episode (same episode_id)
  +-- Trade 1 (episode_trade_sequence=1)
  +-- Trade 2 (episode_trade_sequence=2)  <- same_episode_reentry=true
  +-- Trade 3 (episode_trade_sequence=3)  <- same_episode_reentry=true
```

## ID Format

```text
{DIRECTION}-{DATE}-{TIME}-{RANDOM}
Example: WIDE-20260728-101917-a1b2
```

## Key Rules

1. An episode remains active after trade exit
2. The episode closes only when abs(z) <= reset_z (1.0)
3. OR when z crosses the opposite entry threshold (z <= -2.5 or z >= +2.5)
4. Any re-entry before episode closure = same_episode_reentry=true
5. Episode resets on PM2 restart (in-memory state machine, not persisted)

## Telemetry Fields

| Field                   | Description                              |
|-------------------------|------------------------------------------|
| episode_id              | Unique episode identifier                |
| episode_direction       | WIDE or NARROW                           |
| episode_state           | NEUTRAL, WIDE_ACTIVE, NARROW_ACTIVE      |
| episode_started_at      | When episode first triggered             |
| episode_start_z         | Z-score at episode start                 |
| episode_peak_abs_z      | Highest |z| seen in this episode         |
| episode_reset_z         | Reset threshold (1.0)                    |
| episode_trade_sequence  | Nth trade within this episode            |
| same_episode_reentry    | true if not first trade in episode       |

## Implementation

Located in `strategies/futures/mts/telemetry/experiment_hook.py`:

- `_update_episode(z, event_time)` -- called on every release decision
- `get_episode_snapshot()` -- returns current episode state dict
- Automatically appended to every telemetry event via **episode dict

## 10:19 Case Study

```text
z=4.7  -> trading entry
        ...
z=2.53 -> trading entry (12ms after previous exit)
```

If z never dropped below 1.0 between those two points:
Result: same_episode_reentry = true, single episode with 2+ trades
