#!/usr/bin/env python3
"""
Flight Scenario Simulator for Laser Power Beaming
Generates realistic distance, altitude, and attitude data for different flight profiles
"""

import numpy as np
from dataclasses import dataclass
from typing import Tuple
from abc import ABC, abstractmethod
import matplotlib.pyplot as plt


@dataclass
class DroneState:
    """Complete drone state at a given time"""
    time_s: float
    # Position (relative to home/ground station)
    position_north_m: float
    position_east_m: float
    altitude_m: float
    # Attitude
    roll_deg: float
    pitch_deg: float
    yaw_deg: float
    # Velocity
    velocity_north_ms: float
    velocity_east_ms: float
    velocity_down_ms: float
    # Derived
    distance_horizontal_m: float  # 2D distance from home
    distance_3d_m: float  # 3D distance from ground station

    def __post_init__(self):
        """Calculate derived quantities"""
        self.distance_horizontal_m = np.sqrt(self.position_north_m**2 + self.position_east_m**2)
        self.distance_3d_m = np.sqrt(self.position_north_m**2 + self.position_east_m**2 + self.altitude_m**2)


class FlightScenario(ABC):
    """Base class for flight scenarios"""
    
    def __init__(self, name: str, duration_s: float, dt: float = 0.1):
        self.name = name
        self.duration_s = duration_s
        self.dt = dt  # Time step for state generation
        self.times = np.arange(0, duration_s, dt)
        
    @abstractmethod
    def get_state(self, t: float) -> DroneState:
        """Get drone state at time t"""
        pass
    
    def generate_trajectory(self) -> list[DroneState]:
        """Generate complete trajectory"""
        return [self.get_state(t) for t in self.times]
    
    def get_summary(self) -> dict:
        """Get scenario summary statistics"""
        traj = self.generate_trajectory()
        return {
            'name': self.name,
            'duration_s': self.duration_s,
            'max_altitude_m': max(s.altitude_m for s in traj),
            'max_horizontal_distance_m': max(s.distance_horizontal_m for s in traj),
            'max_3d_distance_m': max(s.distance_3d_m for s in traj),
            'avg_distance_m': np.mean([s.distance_3d_m for s in traj]),
            'max_roll_deg': max(abs(s.roll_deg) for s in traj),
            'max_pitch_deg': max(abs(s.pitch_deg) for s in traj),
        }


# ============================================================================
# Scenario 1: Hover Test (Baseline)
# ============================================================================

class HoverScenario(FlightScenario):
    """Simple hover at fixed altitude - baseline performance test"""
    
    def __init__(self, altitude_m: float = 30.0, duration_s: float = 60.0):
        super().__init__("Hover", duration_s)
        self.altitude_m = altitude_m
        self.hover_start_time = 5.0  # 5s to climb
        
    def get_state(self, t: float) -> DroneState:
        # Takeoff phase
        if t < self.hover_start_time:
            alt = (t / self.hover_start_time) * self.altitude_m
            vz = -self.altitude_m / self.hover_start_time  # Negative = up
            pitch = -10.0  # Slight nose down during climb
        else:
            alt = self.altitude_m
            vz = 0.0
            pitch = 0.0
        
        # Small oscillations to simulate real hover (GPS drift, wind)
        alt += 0.5 * np.sin(0.5 * t)
        roll = 2.0 * np.sin(0.3 * t)  # ±2° roll oscillation
        
        return DroneState(
            time_s=t,
            position_north_m=0.0,
            position_east_m=0.0,
            altitude_m=alt,
            roll_deg=roll,
            pitch_deg=pitch,
            yaw_deg=0.0,
            velocity_north_ms=0.0,
            velocity_east_ms=0.0,
            velocity_down_ms=vz,
            distance_horizontal_m=0.0,  # Will be recalculated in __post_init__
            distance_3d_m=0.0          # Will be recalculated in __post_init__
        )


# ============================================================================
# Scenario 2: Vertical Climb/Descent
# ============================================================================

class VerticalProfileScenario(FlightScenario):
    """Climb to max altitude, hover, then descend"""
    
    def __init__(self, min_alt_m: float = 10.0, max_alt_m: float = 100.0, 
                 duration_s: float = 120.0):
        super().__init__("Vertical Profile", duration_s)
        self.min_alt = min_alt_m
        self.max_alt = max_alt_m
        self.climb_rate = 2.0  # m/s
        
        # Phase durations
        self.climb_time = (max_alt_m - min_alt_m) / self.climb_rate
        self.hover_time = 30.0
        self.descent_time = self.climb_time
        
    def get_state(self, t: float) -> DroneState:
        # Phase 1: Climb from min to max
        if t < self.climb_time:
            alt = self.min_alt + self.climb_rate * t
            vz = -self.climb_rate
            pitch = -8.0  # Nose down during climb
            
        # Phase 2: Hover at max
        elif t < self.climb_time + self.hover_time:
            alt = self.max_alt
            vz = 0.0
            pitch = 0.0
            
        # Phase 3: Descend to min
        else:
            t_descent = t - self.climb_time - self.hover_time
            alt = self.max_alt - self.climb_rate * t_descent
            alt = max(alt, self.min_alt)
            vz = self.climb_rate  # Positive = down
            pitch = 5.0  # Nose up during descent
        
        return DroneState(
            time_s=t,
            position_north_m=0.0,
            position_east_m=0.0,
            altitude_m=alt,
            roll_deg=0.0,
            pitch_deg=pitch,
            yaw_deg=0.0,
            velocity_north_ms=0.0,
            velocity_east_ms=0.0,
            velocity_down_ms=vz,
            distance_horizontal_m=0.0,  # Will be recalculated in __post_init__
            distance_3d_m=0.0          # Will be recalculated in __post_init__
        )


# ============================================================================
# Scenario 3: Linear Departure
# ============================================================================

class LinearDepartureScenario(FlightScenario):
    """Fly straight away from ground station"""
    
    def __init__(self, altitude_m: float = 30.0, max_distance_m: float = 250.0,
                 speed_ms: float = 5.0, duration_s: float = 180.0):
        super().__init__("Linear Departure", duration_s)
        self.altitude = altitude_m
        self.max_distance = max_distance_m
        self.speed = speed_ms
        self.climb_time = 5.0
        
    def get_state(self, t: float) -> DroneState:
        # Initial climb
        if t < self.climb_time:
            alt = (t / self.climb_time) * self.altitude
            north = 0.0
            vn = 0.0
            pitch = -10.0
        else:
            alt = self.altitude
            # Fly north at constant speed
            t_flying = t - self.climb_time
            north = min(self.speed * t_flying, self.max_distance)
            vn = self.speed if north < self.max_distance else 0.0
            pitch = -5.0 if vn > 0 else 0.0  # Nose down when moving
        
        return DroneState(
            time_s=t,
            position_north_m=north,
            position_east_m=0.0,
            altitude_m=alt,
            roll_deg=0.0,
            pitch_deg=pitch,
            yaw_deg=0.0,  # Facing north
            velocity_north_ms=vn,
            velocity_east_ms=0.0,
            velocity_down_ms=0.0,
            distance_horizontal_m=0.0,  # Will be recalculated in __post_init__
            distance_3d_m=0.0          # Will be recalculated in __post_init__
        )


# ============================================================================
# Scenario 4: Circular Orbit
# ============================================================================

class CircularOrbitScenario(FlightScenario):
    """Fly in a circle around ground station"""
    
    def __init__(self, altitude_m: float = 30.0, radius_m: float = 50.0,
                 speed_ms: float = 5.0, duration_s: float = 120.0):
        super().__init__("Circular Orbit", duration_s)
        self.altitude = altitude_m
        self.radius = radius_m
        self.speed = speed_ms
        self.angular_velocity = speed_ms / radius_m  # rad/s
        self.climb_time = 5.0
        
    def get_state(self, t: float) -> DroneState:
        # Initial climb and move to orbit start
        if t < self.climb_time:
            alt = (t / self.climb_time) * self.altitude
            north = (t / self.climb_time) * self.radius
            east = 0.0
            roll = 0.0
            pitch = -10.0
            vn = self.radius / self.climb_time
            ve = 0.0
        else:
            alt = self.altitude
            t_orbit = t - self.climb_time
            angle = self.angular_velocity * t_orbit
            
            # Circular path
            north = self.radius * np.cos(angle)
            east = self.radius * np.sin(angle)
            
            # Velocity tangent to circle
            vn = -self.radius * self.angular_velocity * np.sin(angle)
            ve = self.radius * self.angular_velocity * np.cos(angle)
            
            # Bank angle for coordinated turn
            # bank = arctan(v²/rg)
            roll = np.degrees(np.arctan(self.speed**2 / (self.radius * 9.81)))
            roll *= np.sign(ve)  # Bank into turn
            
            pitch = -3.0  # Slight nose down when moving
        
        yaw = np.degrees(np.arctan2(east, north)) + 90  # Tangent to circle
        
        return DroneState(
            time_s=t,
            position_north_m=north,
            position_east_m=east,
            altitude_m=alt,
            roll_deg=roll,
            pitch_deg=pitch,
            yaw_deg=yaw,
            velocity_north_ms=vn,
            velocity_east_ms=ve,
            velocity_down_ms=0.0,
            distance_horizontal_m=0.0,  # Will be recalculated in __post_init__
            distance_3d_m=0.0          # Will be recalculated in __post_init__
        )


# ============================================================================
# Scenario 5: Return-to-Home
# ============================================================================

class ReturnToHomeScenario(FlightScenario):
    """Start far away, return to home while receiving power"""
    
    def __init__(self, start_distance_m: float = 200.0, altitude_m: float = 30.0,
                 speed_ms: float = 8.0, duration_s: float = 120.0):
        super().__init__("Return to Home", duration_s)
        self.start_distance = start_distance_m
        self.altitude = altitude_m
        self.speed = speed_ms
        self.return_time = start_distance_m / speed_ms
        
    def get_state(self, t: float) -> DroneState:
        # Start at max distance, fly home
        distance_remaining = max(0, self.start_distance - self.speed * t)
        
        # Decelerate near home
        if distance_remaining < 10:
            actual_speed = self.speed * (distance_remaining / 10.0)
        else:
            actual_speed = self.speed
        
        return DroneState(
            time_s=t,
            position_north_m=distance_remaining,  # Started north, returning south
            position_east_m=0.0,
            altitude_m=self.altitude,
            roll_deg=0.0,
            pitch_deg=-8.0 if distance_remaining > 1 else 0.0,
            yaw_deg=180.0,  # Facing home (south)
            velocity_north_ms=-actual_speed,
            velocity_east_ms=0.0,
            velocity_down_ms=0.0,
            distance_horizontal_m=0.0,  # Will be recalculated in __post_init__
            distance_3d_m=0.0          # Will be recalculated in __post_init__
        )


# ============================================================================
# Scenario 6: Aggressive Maneuver
# ============================================================================

class AggressiveManeuverScenario(FlightScenario):
    """Rapid direction changes and bank angles"""
    
    def __init__(self, altitude_m: float = 30.0, duration_s: float = 90.0):
        super().__init__("Aggressive Maneuver", duration_s)
        self.altitude = altitude_m
        self.climb_time = 5.0
        
    def get_state(self, t: float) -> DroneState:
        # Initial climb
        if t < self.climb_time:
            alt = (t / self.climb_time) * self.altitude
            north = east = 0.0
            vn = ve = 0.0
            roll = pitch = 0.0
        else:
            alt = self.altitude
            t_maneuver = t - self.climb_time
            
            # Figure-8 pattern with aggressive banking
            freq = 0.2  # Hz
            north = 30 * np.sin(2 * np.pi * freq * t_maneuver)
            east = 15 * np.sin(4 * np.pi * freq * t_maneuver)
            
            # Velocities (derivatives)
            vn = 30 * 2 * np.pi * freq * np.cos(2 * np.pi * freq * t_maneuver)
            ve = 15 * 4 * np.pi * freq * np.cos(4 * np.pi * freq * t_maneuver)
            
            # Aggressive roll angles
            roll = 35 * np.sin(4 * np.pi * freq * t_maneuver)
            pitch = -10 * np.sin(2 * np.pi * freq * t_maneuver)
        
        yaw = np.degrees(np.arctan2(ve, vn)) if (vn != 0 or ve != 0) else 0
        
        return DroneState(
            time_s=t,
            position_north_m=north,
            position_east_m=east,
            altitude_m=alt,
            roll_deg=roll,
            pitch_deg=pitch,
            yaw_deg=yaw,
            velocity_north_ms=vn,
            velocity_east_ms=ve,
            velocity_down_ms=0.0,
            distance_horizontal_m=0.0,  # Will be recalculated in __post_init__
            distance_3d_m=0.0          # Will be recalculated in __post_init__
        )


# ============================================================================
# Helper Functions
# ============================================================================

def get_all_scenarios() -> list[FlightScenario]:
    """Get all predefined scenarios"""
    return [
        HoverScenario(),
        VerticalProfileScenario(),
        LinearDepartureScenario(),
        CircularOrbitScenario(),
        ReturnToHomeScenario(),
        AggressiveManeuverScenario(),
    ]


def print_scenario_summary():
    """Print summary of all scenarios"""
    print("="*70)
    print("AVAILABLE FLIGHT SCENARIOS")
    print("="*70)
    
    for scenario in get_all_scenarios():
        summary = scenario.get_summary()
        print(f"\n{summary['name']}:")
        print(f"  Duration: {summary['duration_s']:.0f}s")
        print(f"  Max altitude: {summary['max_altitude_m']:.1f}m")
        print(f"  Max horizontal distance: {summary['max_horizontal_distance_m']:.1f}m")
        print(f"  Max 3D distance: {summary['max_3d_distance_m']:.1f}m")
        print(f"  Avg 3D distance: {summary['avg_distance_m']:.1f}m")
        print(f"  Max roll: ±{summary['max_roll_deg']:.1f}°")
        print(f"  Max pitch: ±{summary['max_pitch_deg']:.1f}°")


if __name__ == '__main__':
    print_scenario_summary()
    
    # Example: Generate and plot a trajectory
    scenario = CircularOrbitScenario()
    traj = scenario.generate_trajectory()
    scenario = AggressiveManeuverScenario()
    traj = scenario.generate_trajectory()
    
    # Check roll angles
    rolls = [s.roll_deg for s in traj]
    print(f"Aggressive Maneuver Roll: min={min(rolls):.1f}° max={max(rolls):.1f}°")
    outside_cone = sum(1 for r in rolls if abs(r) > 12.0)
    print(f"Samples outside ±12° cone: {outside_cone}/{len(rolls)} ({100*outside_cone/len(rolls):.1f}%)")
    
    fig, axes = plt.subplots(2, 2, figsize=(12, 10))
    
    # 2D path - Update attribute names
    ax = axes[0, 0]
    ax.plot([s.position_east_m for s in traj], [s.position_north_m for s in traj])
    ax.plot(0, 0, 'r*', markersize=15, label='Ground Station')
    ax.set_xlabel('East (m)')
    ax.set_ylabel('North (m)')
    ax.set_title('Flight Path (Top View)')
    ax.grid(True)
    ax.axis('equal')
    ax.legend()
    
    # Altitude profile - Already correct
    ax = axes[0, 1]
    ax.plot([s.time_s for s in traj], [s.altitude_m for s in traj])
    ax.set_xlabel('Time (s)')
    ax.set_ylabel('Altitude (m)')
    ax.set_title('Altitude Profile')
    ax.grid(True)
    
    # Distance from ground station - Already correct
    ax = axes[1, 0]
    ax.plot([s.time_s for s in traj], [s.distance_3d_m for s in traj])
    ax.set_xlabel('Time (s)')
    ax.set_ylabel('3D Distance (m)')
    ax.set_title('Distance from Ground Station')
    ax.grid(True)
    
    # Attitude - Add missing subplot
    ax = axes[1, 1]
    ax.plot([s.time_s for s in traj], [s.roll_deg for s in traj], label='Roll')
    ax.plot([s.time_s for s in traj], [s.pitch_deg for s in traj], label='Pitch')
    ax.plot([s.time_s for s in traj], [s.yaw_deg for s in traj], label='Yaw')
    ax.set_xlabel('Time (s)')
    ax.set_ylabel('Angle (deg)')
    ax.set_title('Attitude')
    ax.grid(True)
    ax.legend()
    
    plt.tight_layout()
    plt.show()
