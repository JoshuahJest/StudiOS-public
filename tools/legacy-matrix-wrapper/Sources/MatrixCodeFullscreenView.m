#import <AppKit/AppKit.h>
#import <ScreenSaver/ScreenSaver.h>

static NSString * const LegacySaverPath = @"/Library/Screen Savers/3D Matrix Code.saver";

@interface MatrixCodeFullscreenView : ScreenSaverView
@property(nonatomic, strong) ScreenSaverView *legacyView;
@end

@implementation MatrixCodeFullscreenView

- (instancetype)initWithFrame:(NSRect)frame isPreview:(BOOL)isPreview {
    self = [super initWithFrame:frame isPreview:isPreview];
    if (!self) return nil;

    NSBundle *legacyBundle = [NSBundle bundleWithPath:LegacySaverPath];
    NSError *error = nil;
    if (![legacyBundle loadAndReturnError:&error]) {
        NSLog(@"Unable to load legacy Matrix bundle: %@", error);
        return self;
    }

    Class legacyClass = NSClassFromString(@"Matrix3DSSView");
    if (!legacyClass || ![legacyClass isSubclassOfClass:[ScreenSaverView class]]) {
        NSLog(@"Legacy Matrix principal class unavailable");
        return self;
    }

    self.legacyView = [[legacyClass alloc] initWithFrame:self.bounds isPreview:isPreview];
    [self addSubview:self.legacyView];
    [self resizeLegacyView];
    [self setAnimationTimeInterval:self.legacyView.animationTimeInterval ?: 1.0 / 30.0];
    return self;
}

- (void)resizeLegacyView {
    if (!self.legacyView) return;
    self.legacyView.frame = self.bounds;
    // Tahoe presents this 2012 OpenGL saver at half its intended dimensions.
    // A half-sized bounds rect asks AppKit to scale the legacy renderer to the
    // full host view while retaining the original saver binary unchanged.
    self.legacyView.bounds = NSMakeRect(0, 0, NSWidth(self.bounds) / 2.0, NSHeight(self.bounds) / 2.0);
}

- (void)setFrameSize:(NSSize)newSize {
    [super setFrameSize:newSize];
    [self resizeLegacyView];
}

- (void)startAnimation {
    [super startAnimation];
    [self.legacyView startAnimation];
}

- (void)stopAnimation {
    [self.legacyView stopAnimation];
    [super stopAnimation];
}

- (void)animateOneFrame {
    [self.legacyView animateOneFrame];
}

- (BOOL)hasConfigureSheet {
    return [self.legacyView hasConfigureSheet];
}

- (NSWindow *)configureSheet {
    return [self.legacyView configureSheet];
}

@end
